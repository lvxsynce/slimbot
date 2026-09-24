"""Локальный полнотекстовый индекс для `.ии база`.

SQLite FTS5 входит в стандартную библиотеку Python и позволяет не зависеть от
внешнего сервиса. Все записи привязаны к владельцу Telethon-сессии.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from config import KNOWLEDGE_DB_FILE


_LOCK = threading.RLock()
# Path-aware: тесты monkeypatch'ят KNOWLEDGE_DB_FILE на tmp_path, поэтому
# «один раз инициализировали» не может быть module-level — ключ — путь к БД.
_INITIALIZED: dict[str, bool] = {}


def _path(path: Path | None) -> Path:
    return Path(path or KNOWLEDGE_DB_FILE)


@contextmanager
def _connect(path: Path | None = None):
    """Соединение с явным commit/close после блока (до этого каждый вызов
    держал соединение до сборки мусора — на 500MB-базе это сотни живых conn;
    без явного commit все записи молча откатывались при close)."""
    conn = sqlite3.connect(_path(path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize(path: Path | None = None) -> None:
    """Создаёт схему (один раз на путь). Отдельная FTS-таблица упрощает
    portable SQLite-upsert."""
    key = str(_path(path))
    if _INITIALIZED.get(key):
        return
    with _LOCK:
        if _INITIALIZED.get(key):
            return
        with _connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kb_messages (
                    owner_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL DEFAULT 0,
                    message_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    chat_title TEXT NOT NULL DEFAULT '',
                    chat_username TEXT NOT NULL DEFAULT '',
                    sender_id INTEGER,
                    sender_name TEXT NOT NULL DEFAULT '',
                    sent_at TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, chat_id, thread_id, message_id)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS kb_messages_fts USING fts5(
                    text, chat_title, sender_name,
                    content='kb_messages', content_rowid='rowid',
                    tokenize='unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS kb_messages_ai AFTER INSERT ON kb_messages BEGIN
                    INSERT INTO kb_messages_fts(rowid, text, chat_title, sender_name)
                    VALUES (new.rowid, new.text, new.chat_title, new.sender_name);
                END;
                CREATE TRIGGER IF NOT EXISTS kb_messages_ad AFTER DELETE ON kb_messages BEGIN
                    INSERT INTO kb_messages_fts(kb_messages_fts, rowid, text, chat_title, sender_name)
                    VALUES ('delete', old.rowid, old.text, old.chat_title, old.sender_name);
                END;
                CREATE TRIGGER IF NOT EXISTS kb_messages_au AFTER UPDATE ON kb_messages BEGIN
                    INSERT INTO kb_messages_fts(kb_messages_fts, rowid, text, chat_title, sender_name)
                    VALUES ('delete', old.rowid, old.text, old.chat_title, old.sender_name);
                    INSERT INTO kb_messages_fts(rowid, text, chat_title, sender_name)
                    VALUES (new.rowid, new.text, new.chat_title, new.sender_name);
                END;

                CREATE TABLE IF NOT EXISTS kb_dialogs (
                    owner_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    snapshot_top_id INTEGER NOT NULL DEFAULT 0,
                    oldest_processed_id INTEGER NOT NULL DEFAULT 0,
                    latest_seen_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, chat_id)
                );

                CREATE TABLE IF NOT EXISTS kb_collections (
                    owner_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    total_dialogs INTEGER NOT NULL DEFAULT 0,
                    completed_dialogs INTEGER NOT NULL DEFAULT 0,
                    indexed_messages INTEGER NOT NULL DEFAULT 0,
                    current_chat_id INTEGER,
                    current_chat_title TEXT NOT NULL DEFAULT '',
                    progress_message_id INTEGER,
                    paused_until TEXT,
                    error TEXT NOT NULL DEFAULT ''
                );
                """
            )
        _INITIALIZED[key] = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_message(
    owner_id: str,
    *,
    chat_id: int,
    thread_id: int,
    message_id: int,
    text: str,
    chat_title: str = "",
    chat_username: str = "",
    sender_id: int | None = None,
    sender_name: str = "",
    sent_at: str = "",
) -> bool:
    """Сохраняет или обновляет текст. Возвращает True только для новой записи."""
    clean = (text or "").strip()
    if not clean:
        return False
    initialize()
    with _LOCK, _connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM kb_messages WHERE owner_id=? AND chat_id=? AND thread_id=? AND message_id=?",
            (str(owner_id), int(chat_id), int(thread_id), int(message_id)),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO kb_messages (
                owner_id, chat_id, thread_id, message_id, text, chat_title,
                chat_username, sender_id, sender_name, sent_at, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, chat_id, thread_id, message_id) DO UPDATE SET
                text=excluded.text, chat_title=excluded.chat_title,
                chat_username=excluded.chat_username, sender_id=excluded.sender_id,
                sender_name=excluded.sender_name, sent_at=excluded.sent_at,
                indexed_at=excluded.indexed_at
            """,
            (str(owner_id), int(chat_id), int(thread_id), int(message_id), clean,
             chat_title or "", chat_username or "", sender_id, sender_name or "",
             sent_at or "", _now()),
        )
        return existing is None


def search(owner_id: str, query: str, limit: int = 8, chat_ids: set[int] | None = None) -> list[dict]:
    """Ищет только по данным владельца. Fallback защищает от FTS-синтаксиса."""
    words = [part for part in (query or "").split() if part]
    if not words:
        return []
    if chat_ids is not None and not chat_ids:
        return []
    initialize()
    match = " OR ".join(f'"{word.replace(chr(34), "")}"' for word in words[:12])
    chat_filter = ""
    params: list = [match, str(owner_id)]
    if chat_ids is not None:
        placeholders = ", ".join("?" for _ in chat_ids)
        chat_filter = f" AND m.chat_id IN ({placeholders})"
        params.extend(sorted(chat_ids))
    sql = """
        SELECT m.*, bm25(kb_messages_fts) AS rank
        FROM kb_messages_fts
        JOIN kb_messages AS m ON m.rowid=kb_messages_fts.rowid
        WHERE kb_messages_fts MATCH ? AND m.owner_id=?""" + chat_filter + """
        ORDER BY rank, m.sent_at DESC
        LIMIT ?
    """
    params.append(int(limit))
    with _LOCK, _connect() as conn:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            pattern = "%" + "%".join(words[:5]) + "%"
            fallback_filter = ""
            fallback_params: list = [str(owner_id), pattern]
            if chat_ids is not None:
                placeholders = ", ".join("?" for _ in chat_ids)
                fallback_filter = f" AND chat_id IN ({placeholders})"
                fallback_params.extend(sorted(chat_ids))
            fallback_params.append(int(limit))
            rows = conn.execute(
                "SELECT *, 0 AS rank FROM kb_messages WHERE owner_id=? AND text LIKE ?"
                + fallback_filter + " ORDER BY sent_at DESC LIMIT ?",
                fallback_params,
            ).fetchall()
    return [dict(row) for row in rows]


def count_messages(owner_id: str) -> int:
    initialize()
    with _LOCK, _connect() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM kb_messages WHERE owner_id=?", (str(owner_id),)
        ).fetchone()[0])


def get_collection(owner_id: str) -> dict | None:
    initialize()
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM kb_collections WHERE owner_id=?", (str(owner_id),)
        ).fetchone()
    return dict(row) if row else None


def begin_collection(owner_id: str, *, progress_message_id: int | None = None) -> None:
    initialize()
    now = _now()
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO kb_collections(owner_id,status,started_at,updated_at,progress_message_id)
               VALUES (?, 'running', ?, ?, ?)
               ON CONFLICT(owner_id) DO UPDATE SET status='running', updated_at=excluded.updated_at,
                 progress_message_id=COALESCE(excluded.progress_message_id, progress_message_id),
                 paused_until=NULL, error=''""",
            (str(owner_id), now, now, progress_message_id),
        )


def update_collection(owner_id: str, **fields) -> None:
    if not fields:
        return
    initialize()
    fields["updated_at"] = _now()
    cols = ", ".join(f"{key}=?" for key in fields)
    with _LOCK, _connect() as conn:
        conn.execute(
            f"UPDATE kb_collections SET {cols} WHERE owner_id=?",
            (*fields.values(), str(owner_id)),
        )


def upsert_dialog(owner_id: str, chat_id: int, **fields) -> None:
    initialize()
    defaults = {
        "title": "", "username": "", "status": "pending", "snapshot_top_id": 0,
        "oldest_processed_id": 0, "latest_seen_id": 0,
    }
    defaults.update(fields)
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO kb_dialogs(owner_id,chat_id,title,username,status,snapshot_top_id,
                   oldest_processed_id,latest_seen_id,updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_id,chat_id) DO UPDATE SET title=excluded.title,
                 username=excluded.username,status=excluded.status,
                 snapshot_top_id=excluded.snapshot_top_id,
                 oldest_processed_id=excluded.oldest_processed_id,
                 latest_seen_id=excluded.latest_seen_id,updated_at=excluded.updated_at""",
            (str(owner_id), int(chat_id), defaults["title"], defaults["username"],
             defaults["status"], int(defaults["snapshot_top_id"]),
             int(defaults["oldest_processed_id"]), int(defaults["latest_seen_id"]), _now()),
        )


def get_dialog(owner_id: str, chat_id: int) -> dict | None:
    initialize()
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM kb_dialogs WHERE owner_id=? AND chat_id=?",
            (str(owner_id), int(chat_id)),
        ).fetchone()
    return dict(row) if row else None


def prune_owner(owner_id: str, keep_n: int) -> int:
    """Retention: оставляет последние keep_n сообщений владельца, остальное
    удаляет. FTS-таблица синхронизируется триггером kb_messages_ad.

    Возвращает число удалённых строк. Вызывается на чекпоинтах сбора, чтобы
    база знаний не росла бесконечно (см. KNOWLEDGE_MAX_MESSAGES_PER_OWNER).
    """
    if keep_n <= 0:
        return 0
    initialize()
    with _LOCK, _connect() as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM kb_messages WHERE owner_id=?", (str(owner_id),)
        ).fetchone()[0])
        if total <= keep_n:
            return 0
        cursor = conn.execute(
            """DELETE FROM kb_messages WHERE owner_id=? AND rowid NOT IN (
                   SELECT rowid FROM kb_messages WHERE owner_id=?
                   ORDER BY rowid DESC LIMIT ?
               )""",
            (str(owner_id), str(owner_id), int(keep_n)),
        )
        return int(cursor.rowcount)


def clear_owner(owner_id: str) -> None:
    """Полный сброс данных владельца (при logout). После DELETE запускает
    VACUUM, чтобы физически вернуть место системе — иначе 500MB-файл БД
    остаётся «убитым» за счёт deleted-страниц."""
    initialize()
    with _LOCK:
        with _connect() as conn:
            conn.execute("DELETE FROM kb_messages WHERE owner_id=?", (str(owner_id),))
            conn.execute("DELETE FROM kb_dialogs WHERE owner_id=?", (str(owner_id),))
            conn.execute("DELETE FROM kb_collections WHERE owner_id=?", (str(owner_id),))
        try:
            # VACUUM нельзя внутри транзакции — отдельное соединение, после commit.
            with _connect() as conn:
                conn.execute("VACUUM")
        except Exception:
            # VACUUM на большой БД может упереться в диск/lock — не критично.
            pass
