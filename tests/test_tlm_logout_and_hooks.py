"""P2.6/P2.7/P2.8 — logout-честность, независимость `.tr auto` и `.ня`, guard.

- P2.6: `logout()` возвращал безусловный `True`, поэтому UI всегда показывал
  «сессия отозвана в Telegram» даже когда `log_out()` упал.
- P2.7: в `_handle_outgoing` первая ветка (auto-tr) делала `return`, из-за чего
  `.ня` в том же чате был недостижим.
- P2.8: `_auto_tr_messages` снимался на ВТОРОМ dispatch'е, пока первая задача
  ещё жила; TTL/sweeper отсутствовал.
"""

import asyncio

import pytest

from utils import telethon_manager as TLM
from utils.telethon_manager import TelethonManager


@pytest.fixture
def mgr():
    m = TelethonManager()
    yield m
    m._auto_tr_messages.clear()
    m._auto_tr_seen.clear()


# --------------------------------------------------------------------------
# P2.6 — logout возвращает реальный результат revoke
# --------------------------------------------------------------------------

class FakeLogoutClient:
    def __init__(self, log_out_error=None, authorized=True):
        self._log_out_error = log_out_error
        self._authorized = authorized
        self.logged_out = False
        self.disconnected = False

    async def log_out(self):
        if self._log_out_error:
            raise self._log_out_error
        self.logged_out = True

    async def disconnect(self):
        self.disconnected = True


def test_logout_returns_true_when_revoked(monkeypatch, tmp_path):
    m = TelethonManager()
    monkeypatch.setattr(TLM, "session_path", lambda uid: str(tmp_path / uid))
    monkeypatch.setattr(TLM, "user_sessions", {})
    monkeypatch.setattr(TLM, "save_user_sessions", lambda: None)
    client = FakeLogoutClient()
    m._clients["1"] = client
    assert asyncio.run(m.logout("1")) is True
    assert client.logged_out is True


def test_logout_returns_false_when_log_out_fails(monkeypatch, tmp_path):
    """Регрессия: раньше был безусловный `return True`."""
    m = TelethonManager()
    monkeypatch.setattr(TLM, "session_path", lambda uid: str(tmp_path / uid))
    monkeypatch.setattr(TLM, "user_sessions", {})
    monkeypatch.setattr(TLM, "save_user_sessions", lambda: None)
    client = FakeLogoutClient(log_out_error=RuntimeError("AUTH_KEY_UNREGISTERED"))
    m._clients["1"] = client
    assert asyncio.run(m.logout("1")) is False
    assert client.logged_out is False


def test_logout_clears_client_even_when_log_out_fails(monkeypatch, tmp_path):
    m = TelethonManager()
    monkeypatch.setattr(TLM, "session_path", lambda uid: str(tmp_path / uid))
    monkeypatch.setattr(TLM, "user_sessions", {})
    monkeypatch.setattr(TLM, "save_user_sessions", lambda: None)
    client = FakeLogoutClient(log_out_error=RuntimeError("boom"))
    m._clients["1"] = client
    asyncio.run(m.logout("1"))
    assert "1" not in m._clients, "client must be removed regardless of log_out outcome"
    assert client.disconnected is True


# --------------------------------------------------------------------------
# P2.7 — .tr auto и .ня не глушат друг друга
# --------------------------------------------------------------------------

class FakeEvent:
    def __init__(self, text="привет", chat_id=-100, msg_id=1):
        self.raw_text = text
        self.chat_id = chat_id
        self.id = msg_id
        self.edits = []
        self.is_private = False

    async def edit(self, text, **kwargs):
        self.edits.append(text)


@pytest.fixture
def both_features(monkeypatch):
    """Включены ОБЕ фичи в одном чате."""
    monkeypatch.setattr(TLM, "is_auto_tr_chat", lambda uid, cid: True)
    monkeypatch.setattr(TLM, "is_nya_chat", lambda uid, cid: True)
    monkeypatch.setattr(TLM, "get_knowledge_selected_chats", lambda uid: set())


def test_nya_is_not_shadowed_by_auto_tr(monkeypatch, mgr, both_features):
    """Ядро P2.7: обе задачи должны быть созданы."""
    spawned = []

    async def fake_auto_tr(uid, event, text):
        pass

    async def fake_nya(uid, event, text):
        pass

    monkeypatch.setattr(mgr, "_apply_auto_tr_outgoing", fake_auto_tr)
    monkeypatch.setattr(mgr, "_apply_nya", fake_nya)
    real_create = asyncio.create_task

    def spy(coro, **kw):
        spawned.append(getattr(coro, "__qualname__", "?"))
        return real_create(coro, **kw)

    monkeypatch.setattr(asyncio, "create_task", spy)

    event = FakeEvent()
    asyncio.run(mgr._handle_outgoing("1", event))
    assert len(spawned) == 2, f"expected auto-tr AND nya tasks, got {spawned}"


def test_nya_alone_still_works(monkeypatch, mgr):
    monkeypatch.setattr(TLM, "is_auto_tr_chat", lambda uid, cid: False)
    monkeypatch.setattr(TLM, "is_nya_chat", lambda uid, cid: True)
    monkeypatch.setattr(TLM, "get_knowledge_selected_chats", lambda uid: set())
    spawned = []
    real_create = asyncio.create_task

    def spy(coro, **kw):
        spawned.append(getattr(coro, "__qualname__", "?"))
        return real_create(coro, **kw)

    monkeypatch.setattr(asyncio, "create_task", spy)
    asyncio.run(mgr._handle_outgoing("1", FakeEvent()))
    assert len(spawned) == 1


def test_neither_feature_spawns_nothing(monkeypatch, mgr):
    monkeypatch.setattr(TLM, "is_auto_tr_chat", lambda uid, cid: False)
    monkeypatch.setattr(TLM, "is_nya_chat", lambda uid, cid: False)
    monkeypatch.setattr(TLM, "get_knowledge_selected_chats", lambda uid: set())
    spawned = []
    monkeypatch.setattr(
        asyncio, "create_task", lambda coro, **kw: spawned.append(coro)
    )
    asyncio.run(mgr._handle_outgoing("1", FakeEvent()))
    assert not spawned


# --------------------------------------------------------------------------
# P2.8 — guard держится, пока задача жива
# --------------------------------------------------------------------------

def test_duplicate_dispatch_does_not_spawn_second_task(monkeypatch, mgr, both_features):
    """Регрессия: старый код на втором dispatch снимал guard и спавнил вторую
    конкурентную задачу, чей except мог откатить перевод первой."""
    spawned = []
    real_create = asyncio.create_task

    async def never(*a, **kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(mgr, "_apply_auto_tr_outgoing", never)
    monkeypatch.setattr(mgr, "_apply_nya", never)

    def spy(coro, **kw):
        task = real_create(coro, **kw)
        spawned.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", spy)

    async def scenario():
        ev = FakeEvent(msg_id=7)
        await mgr._handle_outgoing("1", ev)
        await mgr._handle_outgoing("1", ev)  # то же собыщение, второй раз
        for t in spawned:
            t.cancel()
        await asyncio.gather(*spawned, return_exceptions=True)

    asyncio.run(scenario())
    auto_tr_tasks = [t for t in spawned if "auto-tr" in (t.get_name() or "")]
    assert len(auto_tr_tasks) == 1, f"expected exactly 1 auto-tr task, got {len(auto_tr_tasks)}"


def test_guard_key_is_released_after_task_finishes(monkeypatch, mgr, both_features):
    key = ("1", -100, 5)
    mgr._auto_tr_messages.add(key)
    mgr._auto_tr_seen[key] = 0.0

    class Ev:
        chat_id = -100
        id = 5
        raw_text = "привет"
        is_private = False

        async def edit(self, text, **kw):
            pass

    # Прогоняем реальную задачу, которая должна снять ключ в finally.
    async def scenario():
        try:
            await mgr._apply_auto_tr_outgoing("1", Ev(), "привет")
        except Exception:
            pass

    monkeypatch.setattr(
        "handlers.commands.tools.translate_with_ai",
        lambda *a, **kw: asyncio.sleep(0, result=("", "boom")),
    )
    asyncio.run(scenario())
    assert key not in mgr._auto_tr_messages
    assert key not in mgr._auto_tr_seen


def test_sweep_drops_stale_keys(mgr):
    import time

    stale = ("1", -100, 1)
    fresh = ("1", -100, 2)
    mgr._auto_tr_messages.update({stale, fresh})
    mgr._auto_tr_seen[stale] = time.monotonic() - 1000
    mgr._auto_tr_seen[fresh] = time.monotonic()

    dropped = mgr.sweep_auto_tr_messages(max_age=300.0)
    assert dropped == 1
    assert stale not in mgr._auto_tr_messages
    assert fresh in mgr._auto_tr_messages
    assert stale not in mgr._auto_tr_seen


def test_sweep_is_noop_when_all_fresh(mgr):
    import time

    key = ("1", -100, 1)
    mgr._auto_tr_messages.add(key)
    mgr._auto_tr_seen[key] = time.monotonic()
    assert mgr.sweep_auto_tr_messages(max_age=300.0) == 0
    assert key in mgr._auto_tr_messages


def test_sweep_runs_as_part_of_health_check(monkeypatch, mgr):
    """Свип должен вызываться из health-check, иначе зависшие ключи копятся."""
    import inspect

    src = inspect.getsource(mgr.check_clients_health)
    assert "sweep_auto_tr_messages" in src
