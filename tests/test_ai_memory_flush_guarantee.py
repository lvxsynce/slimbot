"""P8.1/P8.2/P8.3 — `ai_memory.flush()` ОБЯЗАН гарантировать запись.

Адверсарная проверка нашла три связанных бага в первой версии фикса:

1. `_flush_task` никогда не сбрасывался в `None` ⇒ ветка восстановления в
   `flush()` была недостижимой, `_dirty` навсегда оставался `True`, а
   хвостовая дельта (мутация, случившаяся ПОСЛЕ снятия снапшота) терялась.
2. `_schedule_save` при ЖИВОЙ задаче не планировал новую — дельта осиротевала.
3. `_write_now` сбрасывала `_dirty` ДО записи ⇒ транзиентная ошибка (ENOSPC)
   навсегда теряла изменения.

Плюс дедлок: `await _write_now_async()` находился внутри `with _lock`, а
`_write_now` брал тот же RLock в worker-треде — RLock повторяем только в
одном потоке, это гарантированный дедлок.
"""

import asyncio
import json
import pathlib
import threading

import pytest

from utils import ai_memory as M


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "ai_memory.json"
    monkeypatch.setattr(M, "AI_MEMORY_FILE", path)
    M._history.clear()
    M._metadata.clear()
    M._dirty = False
    M._flush_task = None
    yield path
    if M._flush_task is not None and not M._flush_task.done():
        M._flush_task.cancel()
    M._history.clear()
    M._metadata.clear()
    M._flush_task = None


def _on_disk(path) -> list:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("conversations", {}).get("u1:10:0", {}).get("turns", [])


# --------------------------------------------------------------------------
# P8.1 — flush() гарантирует запись ВСЕГО
# --------------------------------------------------------------------------

def test_flush_persists_everything_added_before_it():
    async def scenario():
        M.add_exchange("u1", 10, "q1", "a1")
        M.add_exchange("u1", 10, "q2", "a2")
        await M.flush()

    path = pathlib.Path(M.AI_MEMORY_FILE)
    asyncio.run(scenario())
    assert len(_on_disk(path)) == 4, f"expected 4 turns, got {_on_disk(path)}"


def test_mutation_during_write_is_not_lost():
    """Главный сценарий: мутация ПРЯМО ВО ВРЕМЯ записи.

    Первая версия фикса теряла этот хвост — `_dirty` снимался до I/O, а
    вторая правка не планировалась, пока первая задача жива.
    """
    path = pathlib.Path(M.AI_MEMORY_FILE)
    loop_done = threading.Event()
    real_write = M._write_now

    def slow_write():
        if not loop_done.is_set():
            real_write()      # пишем первый снапшот
            # пока файл пишется, юзер задаёт ещё один вопрос
            M._history[("u1", 10, 0)] = [
                {"role": "user", "content": "late-q"},
                {"role": "assistant", "content": "late-a"},
            ]
            M._mark_dirty()
            loop_done.set()
            import time as _t
            _t.sleep(0.05)   # «медленный диск»
        else:
            real_write()      # повторный проход дописывает хвост

    async def scenario():
        monkey = slow_write
        orig = M._write_now
        M._write_now = monkey
        try:
            M.add_exchange("u1", 10, "q1", "a1")
            await M.flush()
        finally:
            M._write_now = orig

    asyncio.run(scenario())
    assert loop_done.is_set()
    turns = _on_disk(path)
    contents = [t["content"] for t in turns]
    assert "late-a" in contents, f"tail mutation lost: {contents}"
    assert M._dirty is False, "flush() must converge: _dirty cleared"


def test_flush_converges_and_clears_dirty():
    async def scenario():
        M.add_exchange("u1", 10, "q", "a")
        await M.flush()
        return M._dirty

    assert asyncio.run(scenario()) is False


def test_flush_task_slot_is_released():
    """Слот задачи обязан освобождаться, иначе он «залипает» навсегда."""
    async def scenario():
        M.add_exchange("u1", 10, "q", "a")
        await M.flush()
        return M._flush_task

    assert asyncio.run(scenario()) is None, "_flush_task must be reset to None"


def test_repeated_flush_without_changes_is_cheap():
    async def scenario():
        M.add_exchange("u1", 10, "q", "a")
        await M.flush()
        import time as _t
        start = _t.monotonic()
        for _ in range(50):
            await M.flush()
        return _t.monotonic() - start

    assert asyncio.run(scenario()) < 0.5, "idle flush() must be a cheap no-op"


# --------------------------------------------------------------------------
# P8.2 — падение записи не теряет дельту
# --------------------------------------------------------------------------

def test_failed_write_keeps_dirty_flag(monkeypatch):
    """_write_now сбрасывал _dirty ДО I/O: ENOSPC ⇒ дельта потеряна навсегда."""
    calls = {"n": 0}

    def failing_write():
        calls["n"] += 1
        if calls["n"] == 1:
            M._mark_dirty()
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(M, "_write_now", failing_write)

    async def scenario():
        M.add_exchange("u1", 10, "q", "a")
        await M.flush()

    asyncio.run(scenario())
    # Флаг обязан остаться взведённым, чтобы следующая попытка записала.
    assert M._dirty is True, "failed write must keep _dirty so the delta is retried"


def test_real_write_failure_keeps_flag(monkeypatch, tmp_path):
    """Тот же сценарий на настоящем _write_now: путь занят файлом ⇒ mkdir
    падает, и _dirty обязан остаться взведённым для ретрая."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(M, "AI_MEMORY_FILE", blocker / "x.json")
    M._mark_dirty()
    with pytest.raises(OSError):
        M._write_now()
    assert M._dirty is True, "a failed real write must leave _dirty set"


# --------------------------------------------------------------------------
# P8.3 — дедлок
# --------------------------------------------------------------------------

def test_flush_does_not_hold_lock_across_await():
    """Статическая проверка: в flush() нельзя await'ить внутри `with _lock`."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(M.flush)))
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                        # допустим только await to_thread ПОСЛЕ выхода? Нет —
                        # внутри with _lock любой await опасен.
                        src = ast.unparse(sub)
                        raise AssertionError(
                            f"flush(): await/async inside `with` block: {src!r}"
                        )


def test_flush_completes_under_concurrent_mutation():
    """Гонка без дедлока: мутаторы бьют по памяти, пока идёт flush."""
    stop = threading.Event()
    errors = []

    def spam():
        try:
            while not stop.is_set():
                M._history.setdefault(("u1", 10, 0), []).append(
                    {"role": "user", "content": "spam"}
                )
                M._mark_dirty()
                import time as _t
                _t.sleep(0.001)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    async def scenario():
        t = threading.Thread(target=spam, daemon=True)
        t.start()
        try:
            # Спам непрерывен ⇒ flush() не обязан сойтись, он обязан
            # завершиться без дедлока. Проверяем именно это.
            for _ in range(5):
                await M.flush()
        finally:
            stop.set()
            t.join(timeout=2)

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))
    assert not errors, errors
