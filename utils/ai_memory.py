"""Persistent, thread-aware conversation memory for `.ии`."""

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
logger = logging.getLogger(__name__)


def _key(user_id: str, chat_id: int, thread_id: int | None = 0) -> tuple[str, int, int]:
    return str(user_id), int(chat_id), int(thread_id or 0)


def _save() -> None:
    path = Path(AI_MEMORY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


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
    """Append one turn, trim complete history, and persist it atomically."""
    if role not in {"user", "assistant"} or not content:
        return
    key = _key(user_id, chat_id, thread_id)
    with _lock:
        turns = _history.setdefault(key, [])
        turns.append({"role": role, "content": str(content)[:AI_MEMORY_MAX_CHARS]})
        _history[key] = _trim(turns)
        _metadata.setdefault(key, {})["updated_at"] = time.time()
        _trim_topics()
        try:
            _save()
        except OSError:
            logger.exception("Failed to persist AI memory")


def add_exchange(
    user_id: str,
    chat_id: int,
    user_content: str,
    assistant_content: str,
    thread_id: int | None = 0,
) -> None:
    """Store one complete exchange in a single atomic file update."""
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
        try:
            _save()
        except OSError:
            logger.exception("Failed to persist AI memory")


def clear(user_id: str, chat_id: int, thread_id: int | None = 0) -> int:
    """Clear one chat topic and return the number of removed turns."""
    key = _key(user_id, chat_id, thread_id)
    with _lock:
        count = len(_history.pop(key, []))
        _metadata.pop(key, None)
        if count:
            try:
                _save()
            except OSError:
                logger.exception("Failed to persist AI memory")
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
            try:
                _save()
            except OSError:
                logger.exception("Failed to persist AI memory")
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
        try:
            _save()
        except OSError:
            logger.exception("Failed to persist AI memory summary")


def summary(user_id: str, chat_id: int, thread_id: int | None = 0) -> str:
    with _lock:
        return str(_metadata.get(_key(user_id, chat_id, thread_id), {}).get("summary", ""))


_load()
