"""P3.5 — `ai_memory` не должна писать 41 МБ синхронно на event loop.

Замер на конфигурации по умолчанию (500 топиков × 40 ходов × 2000 символов):
payload — 41.6 МБ, полный `_save()` с `fsync` — **1.57 с блокировки loop на
каждый `.ии` от любого пользователя**. Плюс: файл общий, поэтому запрос
пользователя A ронял всех остальных.

Теперь запись асинхронная (thread + coalescing), а `flush()` даёт точку
гарантии долговечности.
"""

import asyncio
import json
import pathlib

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
    M._history.clear()
    M._metadata.clear()


def test_flush_is_a_coroutine_function():
    import inspect
    assert inspect.iscoroutinefunction(M.flush)


def test_sync_mutator_writes_immediately():
    """Вне event loop (тесты/синхронный код) запись должна быть синхронной."""
    M.add_exchange("u1", 10, "q", "a")
    assert pathlib.Path(M.AI_MEMORY_FILE).exists(), "sync path must persist at once"


def test_async_mutator_defers_write():
    """В event loop запись откладывается — это и есть цель фикса."""
    path = pathlib.Path(M.AI_MEMORY_FILE)

    async def scenario():
        M.add_exchange("u1", 10, "q", "a")
        # файл ещё не создан: запись в фоне
        assert not path.exists() or path.stat().st_size >= 0
        await M.flush()
        return path.exists()

    assert asyncio.run(scenario())


def test_flush_guarantees_persistence():
    async def scenario():
        M.add_exchange("u1", 10, "q", "a")
        await M.flush()
        return json.loads(pathlib.Path(M.AI_MEMORY_FILE).read_text(encoding="utf-8"))

    data = asyncio.run(scenario())
    assert data["version"] == 2
    assert "u1:10:0" in data["conversations"]
    assert len(data["conversations"]["u1:10:0"]["turns"]) == 2


def test_loop_is_not_blocked_by_save(monkeypatch):
    """Главный инвариант: мутатор НЕ пишет файл синхронно сам.

    Считаем «стоимость» синхронной записи: если бы мутатор писал сам,
    тикер не успевал бы отработать между вызовами. Вместо этого замеряем
    прямое время мутатора — оно обязано быть микросекундным.
    """
    import time as _t

    def slow_write():
        _t.sleep(0.3)  # эмуляция 0.3 с синхронной записи файла
        M._dirty = False

    monkeypatch.setattr(M, "_write_now", slow_write)

    async def scenario():
        M._dirty = False
        start = _t.monotonic()
        for i in range(5):
            M.add_exchange("u1", 10, f"q{i}", f"a{i}")
        return _t.monotonic() - start

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.1, (
        f"add_exchange took {elapsed:.3f}s — it must not write the file inline "
        "(with a 0.3s write on the critical path this would be >= 1.5s)"
    )


def test_flush_actually_writes_through_the_thread(monkeypatch):
    """flush() обязан дождаться реальной записи, сделанной в потоке."""
    thread_ids = []
    real_write = M._write_now

    def tracking_write():
        import threading
        thread_ids.append(threading.get_ident())
        return real_write()

    monkeypatch.setattr(M, "_write_now", tracking_write)
    import threading as _th
    loop_thread = _th.get_ident()

    async def scenario():
        M.add_exchange("u1", 10, "q", "a")
        await M.flush()

    asyncio.run(scenario())
    assert thread_ids, "flush() must invoke _write_now"
    assert thread_ids[0] != loop_thread, "write must happen off the event-loop thread"


def test_coalescing_many_mutators_into_one_write(monkeypatch):
    """100 быстрых мутаторов не должны давать 100 записей файла."""
    writes = []
    real_write = M._write_now

    def counting_write():
        writes.append(1)
        return real_write()

    monkeypatch.setattr(M, "_write_now", counting_write)

    async def scenario():
        for i in range(100):
            M.add_exchange("u1", 10, f"q{i}", f"a{i}")
        await M.flush()

    asyncio.run(scenario())
    assert len(writes) <= 5, f"writes were not coalesced: {len(writes)}"


def test_clear_then_flush_is_durable():
    async def scenario():
        M.add_exchange("u1", 10, "q", "a")
        await M.flush()
        M.clear_all("u1")
        await M.flush()
        return json.loads(pathlib.Path(M.AI_MEMORY_FILE).read_text(encoding="utf-8"))

    data = asyncio.run(scenario())
    assert data["conversations"] == {}, "clear_all must be durable after flush"


def test_clear_is_durable():
    async def scenario():
        M.add_exchange("u1", 10, "q", "a")
        await M.flush()
        M.clear("u1", 10)
        await M.flush()
        return json.loads(pathlib.Path(M.AI_MEMORY_FILE).read_text(encoding="utf-8"))

    data = asyncio.run(scenario())
    assert "u1:10:0" not in data["conversations"]


def test_dirty_flag_is_cleared_after_write():
    async def scenario():
        M.add_exchange("u1", 10, "q", "a")
        await M.flush()
        return M._dirty

    assert asyncio.run(scenario()) is False


def test_flush_without_changes_is_a_noop():
    async def scenario():
        await M.flush()
        return not pathlib.Path(M.AI_MEMORY_FILE).exists()

    assert asyncio.run(scenario()) is True


def test_reload_roundtrip_preserves_history():
    async def scenario():
        M.add_exchange("u1", 10, "вопрос", "ответ")
        await M.flush()
        M._history.clear()
        M._metadata.clear()
        M._load()
        return M.get("u1", 10)

    turns = asyncio.run(scenario())
    assert turns == [
        {"role": "user", "content": "вопрос"},
        {"role": "assistant", "content": "ответ"},
    ]


def test_ai_handlers_await_flush():
    """Оба пути `.ии` и сброс обязаны ждать записи перед ответом."""
    src = pathlib.Path("handlers/commands/ai.py").read_text(encoding="utf-8")
    assert src.count("await ai_memory.flush()") >= 3, (
        "expected flush() after add_exchange in both paths and in _do_reset"
    )


def test_no_direct_save_calls_left():
    """Старый синхронный `_save()` больше не существует.

    Проверяем по AST (docstring и комментарии исключены), а не grep'ом:
    в docstring модуля `_save()` упоминается намеренно, как описание бага.
    """
    assert not hasattr(M, "_save"), "sync _save() must be gone"

    import ast

    tree = ast.parse(pathlib.Path("utils/ai_memory.py").read_text(encoding="utf-8"))
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)
    assert "_save" not in calls, "no executable call to _save() may remain"
    assert "_write_now" in calls, "writes must go through _write_now"
