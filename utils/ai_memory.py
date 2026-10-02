"""Persistent, thread-aware conversation memory for `.ии`.

Все мутаторы СИНХРОННЫ и вызываются прямо из корутин (ai.py). Поэтому
сохранение обязано быть дешёвым. Раньше ``_save()`` сериализовал весь файл
(до 41.6 МБ на конфигурации по умолчанию) и делал ``fsync`` — 1.57 с
блокировки event loop на КАЖДЫЙ ``.ии`` от ЛЮБОГО пользователя.

Теперь запись идёт в фоне: мутатор только ставит «грязный» флаг, а
coalescing-воркер дописывает файл одним проходом. Публичные мутаторы
возвращают ``True``, если запись была отложена, — вызывающий код может
``await ai_memory.flush()`` в точке, где важна гарантия долговечности
(перед `await` сетевого вызова LLM, например).
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

from config import AI_MEMORY_FILE, AI_MEMORY_MAX_CHARS, AI_MEMORY_MAX_TOPICS, AI_MEMORY_TTL, MAX_HISTORY

# {(user_id, chat_id, thread_id): [{"role": ..., "content": ...}, ...]}
_history: dict[tuple[str, int, int], list[dict]] = {}
_metadata: dict[tuple[str, int, int], dict] = {}
_lock = threading.RLock()
#: Сериализует запись файла. Фоновая задача и `flush()` могут стартовать
#: одновременно; без него они делили один `.tmp`.
_write_lock = threading.Lock()
logger = logging.getLogger(__name__)

# Coalescing: пока файл не записан, новые изменения только взводят флаг.
_dirty = False
_flush_task: asyncio.Task | None = None
#: Сколько раз flush() готов повторить запись, пока флаг не снимется.
#: 3 с запасом: мутация во время записи, упавшая запись, ещё одна мутация.
_FLUSH_ATTEMPTS = 3


def _key(user_id: str, chat_id: int, thread_id: int | None = 0) -> tuple[str, int, int]:
    return str(user_id), int(chat_id), int(thread_id or 0)


def _write_now() -> None:
    """Синхронный проход записи. Вызывается только из потока (см. flush).

    Два инварианта, за которые стоило заплатить:

    * ``_dirty`` сбрасывается ПОСЛЕ успешной записи, а не до: иначе
      транзиентная ошибка (ENOSPC/EIOFBIG) навсегда теряла бы дельту.
    * запись сериализована ``_write_lock``: фоновая задача и ``flush()`` могут
      стартовать одновременно и без лока делили один ``.tmp`` — второй
      ``os.replace`` падал с FileNotFoundError, а данные оставались
      полузаписанными.
    """
    global _dirty
    with _write_lock:
        with _lock:
            if not _dirty:
                return
            # Снимок под замком; сам I/O — уже без замка.
            payload = {
                "version": 2,
                "conversations": {
                    f"{uid}:{cid}:{tid}": {
                        "turns": messages,
                        "summary": _metadata.get((uid, cid, tid), {}).get("summary", ""),
                        "updated_at": _metadata.get((uid, cid, tid), {}).get("updated_at", time.time()),
                    }
                    for (uid, cid, tid), messages in _history.items()
                },
            }

        path = Path(AI_MEMORY_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            # Не оставляем мусорный .tmp, но сохраняем _dirty для ретрая.
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

        # Только теперь состояние на диске совпадает со снимком. Если мутация
        # успела произойти ПОСЛЕ снятия снапшота, её флаг снова взведён — и
        # следующий flush() её запишет.
        with _lock:
            _dirty = False


def _mark_dirty() -> None:
    global _dirty
    with _lock:
        _dirty = True


def _take_flush_task() -> "asyncio.Task | None":
    """Забрать текущую задачу записи и сбросить слот (в т.ч. после ошибки)."""
    global _flush_task
    task = _flush_task
    _flush_task = None
    return task


async def flush() -> None:
    """Дождаться, что память на диске.

    Публичные мутаторы (`add_exchange`, `clear`, `clear_all`) НЕ пишут файл
    сами — они только взводят флаг и планируют фоновую задачу. Вызывай
    ``await flush()`` там, где потеря памяти при падении недопустима.

    Гарантия: на выходе из flush() на диске лежит ВСЁ, что было добавлено до
    её вызова. Цикл повторяет, пока флаг остаётся взведённым — мутация,
    случившаяся во время записи, требует ещё одного прохода.
    """
    # ВАЖНО: цикл и все await — ВНЕ `_lock`. RLock повторяем только в одном
    # потоке; удерживать его на время await из loop-треда и тут же брать его
    # в worker-треде — гарантированный дедлок.
    for _ in range(_FLUSH_ATTEMPTS):
        task = _take_flush_task()
        if task is not None and task is not asyncio.current_task():
            try:
                await task
            except Exception:
                logger.exception("ai_memory: background flush failed")

        with _lock:
            still_dirty = _dirty
        if not still_dirty:
            return
        # Что-то изменилось во время записи (или предыдущая упала) — пишем
        # синхронно в потоке, без планирования фоновой задачи.
        await asyncio.to_thread(_write_now)

    with _lock:
        if _dirty:
            logger.error("ai_memory: flush did not converge after %d attempts", _FLUSH_ATTEMPTS)


async def _write_now_async() -> None:
    await asyncio.to_thread(_write_now)


def _schedule_save() -> bool:
    """Взвести флаг и запланировать фоновую запись. True = запись отложена."""
    _mark_dirty()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Вне event loop (тесты, синхронные вызовы) — пишем сразу.
        try:
            _write_now()
        except OSError:
            logger.exception("Failed to persist AI memory")
        return False
    global _flush_task
    # Ключевой момент: если предыдущая запись ЕЩЁ ИДЁТ, новую не планируем —
    # иначе её снапшот устареет и дельта осиротеет. Но тогда запись должна
    # взвести флаг ПОВТОРНО, когда завершится. Поэтому вешаем добровольца,
    # который перезапустит запись, если после его завершения файл всё ещё
    # «грязный».
    if _flush_task is None or _flush_task.done():
        _flush_task = loop.create_task(_write_then_retry())
    return True


async def _write_then_retry() -> None:
    """Фоновая запись + добровольный повтор, если флаг снова взведён."""
    global _flush_task
    try:
        await _write_now_async()
    except OSError:
        logger.exception("Failed to persist AI memory")
    finally:
        # Слот освобождаем ДО проверки: иначе flush() не сможет забрать задачу
        # и увидит «_flush_task не None» вечно.
        if _flush_task is asyncio.current_task():
            _flush_task = None
    with _lock:
        dirty = _dirty
    if dirty:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if _flush_task is None or _flush_task.done():
            _flush_task = loop.create_task(_write_then_retry())


def _trim(messages: list[dict]) -> list[dict]:
    """Keep recent turns while never starting history with an assistant turn."""
    capacity = max(2, MAX_HISTORY - (MAX_HISTORY % 2))
    messages = messages[-capacity:]
    while messages and messages[0]["role"] == "assistant":
        messages.pop(0)
    return messages


def _trim_topics() -> None:
    while len(_history) > AI_MEMORY_MAX_TOPICS:
        key = next(iter(_history))
        _history.pop(key)
        _metadata.pop(key, None)


def _load() -> None:
    global _history, _metadata
    path = Path(AI_MEMORY_FILE)
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        # Migrate the previous flat `{user:chat:thread: turns}` shape in place.
        conversations = raw.get("conversations") if isinstance(raw.get("conversations"), dict) else raw
        loaded: dict[tuple[str, int, int], list[dict]] = {}
        metadata: dict[tuple[str, int, int], dict] = {}
        now = time.time()
        for encoded_key, value in conversations.items():
            messages = value.get("turns", []) if isinstance(value, dict) else value
            parts = str(encoded_key).split(":", 2)
            if len(parts) != 3 or not isinstance(messages, list):
                continue
            key = (parts[0], int(parts[1]), int(parts[2]))
            clean = []
            for item in messages[-MAX_HISTORY:]:
                if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                    continue
                content = item.get("content")
                if isinstance(content, str) and content:
                    clean.append({"role": item["role"], "content": content[:AI_MEMORY_MAX_CHARS]})
            clean = _trim(clean)
            if clean:
                updated_at = float(value.get("updated_at", now)) if isinstance(value, dict) else now
                if AI_MEMORY_TTL > 0 and now - updated_at > AI_MEMORY_TTL:
                    continue
                loaded[key] = clean
                metadata[key] = {
                    "updated_at": updated_at,
                    "summary": str(value.get("summary", ""))[:AI_MEMORY_MAX_CHARS] if isinstance(value, dict) else "",
                }
        _history = loaded
        _metadata = metadata
        _trim_topics()
    except (OSError, ValueError, TypeError):
        _history = {}
        _metadata = {}


def get(user_id: str, chat_id: int, thread_id: int | None = 0) -> list[dict]:
    """Return a copy of memory for one chat topic."""
    with _lock:
        return [dict(item) for item in _history.get(_key(user_id, chat_id, thread_id), [])]


def add(user_id: str, chat_id: int, role: str, content: str, thread_id: int | None = 0) -> None:
    """Append one turn and trim complete history (write is scheduled)."""
    if role not in {"user", "assistant"} or not content:
        return
    key = _key(user_id, chat_id, thread_id)
    with _lock:
        turns = _history.setdefault(key, [])
        turns.append({"role": role, "content": str(content)[:AI_MEMORY_MAX_CHARS]})
        _history[key] = _trim(turns)
        _metadata.setdefault(key, {})["updated_at"] = time.time()
        _trim_topics()
    _schedule_save()


def add_exchange(
    user_id: str,
    chat_id: int,
    user_content: str,
    assistant_content: str,
    thread_id: int | None = 0,
) -> None:
    """Store one complete exchange (write is scheduled, see `flush`)."""
    if not user_content or not assistant_content:
        return
    key = _key(user_id, chat_id, thread_id)
    with _lock:
        turns = _history.setdefault(key, [])
        turns.extend((
            {"role": "user", "content": str(user_content)[:AI_MEMORY_MAX_CHARS]},
            {"role": "assistant", "content": str(assistant_content)[:AI_MEMORY_MAX_CHARS]},
        ))
        _history[key] = _trim(turns)
        # Keep a compact durable index even when the full turn history is
        # later trimmed. This is intentionally extractive, not an extra LLM
        # call: it cannot invent facts or add latency to every request.
        _metadata.setdefault(key, {})["summary"] = (
            f"Последний вопрос: {str(user_content)[:700]}\n"
            f"Последний ответ: {str(assistant_content)[:700]}"
        )
        _metadata.setdefault(key, {})["updated_at"] = time.time()
        _trim_topics()
    _schedule_save()


def clear(user_id: str, chat_id: int, thread_id: int | None = 0) -> int:
    """Clear one chat topic and return the number of removed turns."""
    key = _key(user_id, chat_id, thread_id)
    with _lock:
        count = len(_history.pop(key, []))
        _metadata.pop(key, None)
    if count:
        _schedule_save()
    return count


def clear_all(user_id: str) -> int:
    """Clear every topic belonging to one user and return topic count."""
    uid = str(user_id)
    with _lock:
        keys = [key for key in _history if key[0] == uid]
        for key in keys:
            _history.pop(key, None)
            _metadata.pop(key, None)
    if keys:
        _schedule_save()
    return len(keys)


def size(user_id: str, chat_id: int, thread_id: int | None = 0) -> int:
    with _lock:
        return len(_history.get(_key(user_id, chat_id, thread_id), []))


def total_size(user_id: str) -> int:
    with _lock:
        return sum(len(messages) for key, messages in _history.items() if key[0] == str(user_id))


def chat_count(user_id: str) -> int:
    with _lock:
        return sum(1 for key in _history if key[0] == str(user_id))


def set_summary(user_id: str, chat_id: int, summary: str, thread_id: int | None = 0) -> None:
    """Store a compact durable summary for future context compaction."""
    key = _key(user_id, chat_id, thread_id)
    with _lock:
        _metadata.setdefault(key, {})["summary"] = str(summary)[:AI_MEMORY_MAX_CHARS]
        _metadata[key]["updated_at"] = time.time()
    _schedule_save()


def summary(user_id: str, chat_id: int, thread_id: int | None = 0) -> str:
    with _lock:
        return str(_metadata.get(_key(user_id, chat_id, thread_id), {}).get("summary", ""))


_load()
