"""P3.4 — синхронный SQLite не должен блокировать event loop.

Замеры на реальной базе (525 МБ, 200 647 строк):
  count_messages          — 52 мс (warm) / 114 мс (cold)
  DELETE 200k строк       — 10.3 с
  VACUUM                  —  1.9 с
Всё это выполнялось в корутине, то есть замораживало ВСЕ Telethon-клиенты.

Проверяем два уровня:
1. Динамика — тикер event loop продолжает крутиться во время DB-операций.
2. Статика — ни один async-код не вызывает синхронный `knowledge_db.*`
   напрямую (только через `run_db` / `to_thread`).
"""

import asyncio
import inspect
import pathlib

import pytest

from utils import knowledge_db as K

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_run_db_is_a_coroutine_function():
    assert inspect.iscoroutinefunction(K.run_db)


def test_run_db_executes_and_returns_value():
    assert asyncio.run(K.run_db(K.count_messages, "no-such-owner")) == 0


def test_loop_keeps_spinning_during_reads(tmp_path, monkeypatch):
    """Пока идёт «тяжёлый» COUNT, loop обязан продолжать крутиться.

    На tmpfs база маленькая и запрос быстрый, поэтому задержку имитируем
    явно: проверяем именно МЕХАНИЗМ offload, а не скорость sqlite.
    """
    monkeypatch.setattr(K, "KNOWLEDGE_DB_FILE", tmp_path / "kb.sqlite3")
    K.initialize(tmp_path / "kb.sqlite3")
    for i in range(300):
        K.upsert_message("u1", chat_id=1, thread_id=0, message_id=i, text=f"msg {i}")

    ticks = []

    def slow_count(owner):
        import time as _t
        _t.sleep(0.3)  # имитация реального 0.3 с COUNT на большой базе
        return K.count_messages(owner)

    async def scenario():
        async def ticker():
            for _ in range(40):
                ticks.append(1)
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        total = await K.run_db(slow_count, "u1")
        t.cancel()
        return total

    total = asyncio.run(scenario())
    assert total == 300
    assert len(ticks) >= 5, f"loop was blocked during a DB read (ticks={len(ticks)})"


def test_loop_keeps_spinning_during_clear_owner(tmp_path, monkeypatch):
    """Logout-путь: DELETE по большой базе не должен вставать на loop."""
    monkeypatch.setattr(K, "KNOWLEDGE_DB_FILE", tmp_path / "kb.sqlite3")
    K.initialize(tmp_path / "kb.sqlite3")
    for i in range(2000):
        K.upsert_message("u1", chat_id=1, thread_id=0, message_id=i, text="x" * 50)

    ticks = []

    def slow_clear(owner):
        import time as _t
        _t.sleep(0.3)  # имитация реальных ~10 с DELETE на 200k строк
        K.clear_owner(owner)

    async def scenario():
        async def ticker():
            for _ in range(40):
                ticks.append(1)
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        await asyncio.to_thread(slow_clear, "u1")
        t.cancel()

    asyncio.run(scenario())
    assert len(ticks) >= 5, f"loop was blocked during clear_owner (ticks={len(ticks)})"
    assert K.count_messages("u1") == 0


def test_clear_owner_does_not_vacuum(tmp_path, monkeypatch):
    """VACUUM — глобальный эксклюзивный лок по ВСЕЙ общей базе (1.9 с на
    525 МБ). В logout его быть не должно; отдельная ручная функция осталась."""
    monkeypatch.setattr(K, "KNOWLEDGE_DB_FILE", tmp_path / "kb.sqlite3")
    K.initialize(tmp_path / "kb.sqlite3")
    K.upsert_message("u1", chat_id=1, thread_id=0, message_id=1, text="hello")

    import ast
    import inspect as _i
    import textwrap

    # Вырезаем исполняемый код: убираем docstring (Expr со строковой
    # константой), а не просто grep по тексту.
    tree = ast.parse(textwrap.dedent(_i.getsource(K.clear_owner)))
    stmts = [
        s for s in tree.body[0].body
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    code = ast.unparse(ast.Module(body=stmts, type_ignores=[]))
    assert "VACUUM" not in code, "clear_owner must not execute VACUUM"
    # ручная обслуживающая функция осталась доступна
    assert callable(K.vacuum)
    K.vacuum()  # не должно падать


# --------------------------------------------------------------------------
# Статические инварианты: async-код обязан ходить через run_db
# --------------------------------------------------------------------------

SYNC_FUNCS = (
    "get_collection", "begin_collection", "update_collection", "count_messages",
    "prune_owner", "get_dialog", "upsert_dialog", "upsert_message", "search",
)


@pytest.mark.parametrize("path", ["utils/knowledge_collector.py", "handlers/commands/ai.py"])
def test_async_callers_use_run_db(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    for fn in SYNC_FUNCS:
        # каждое упоминание knowledge_db.<fn> должно идти после run_db(
        needle = f"knowledge_db.{fn}("
        idx = 0
        while True:
            idx = src.find(needle, idx)
            if idx == -1:
                break
            prefix = src[max(0, idx - 40):idx]
            assert "run_db(" in prefix, (
                f"{path}: knowledge_db.{fn}( вызывается без run_db — блокирует loop"
            )
            idx += len(needle)


def test_telethon_logout_offloads_clear_owner():
    src = (ROOT / "utils" / "telethon_manager.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(clear_owner, user_id)" in src, (
        "logout must offload clear_owner off the event loop"
    )


def test_module_docstring_warns_about_sync_api():
    doc = K.__doc__ or ""
    assert "run_db" in doc, "module docstring must point async callers at run_db"
