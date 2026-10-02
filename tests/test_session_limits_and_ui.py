"""P8.9 / P8.10 — лимит сессий реально применяется; allowlist виден в UI.

- P8.9: `MAX_ACTIVE_SESSIONS` был объявлен в config и не читался НИГДЕ.
  `start_all_active` поднимал все сессии одним `asyncio.gather`.
- P8.10: `SESSION_ALLOWLIST` проверялся только в двух местах бэкенда.
  Юзер, которому запрещено, всё равно видел кнопку «Включить» и узнавал об
  отказе только после нажатия.
"""

import asyncio

import pytest

from handlers import session as S
from utils.telethon_manager import TelethonManager


@pytest.fixture
def mgr():
    m = TelethonManager()
    m._clients.clear()
    m._tasks.clear()
    yield m


@pytest.fixture(autouse=True)
def _clean():
    from utils import storage as ST
    saved = dict(ST.user_sessions)
    yield
    ST.user_sessions.clear()
    ST.user_sessions.update(saved)


def _fill(monkeypatch, count):
    from utils import storage as ST
    ST.user_sessions.clear()
    for i in range(count):
        ST.user_sessions[str(i)] = {"phone": "+7", "has_2fa": False, "status": "active"}


# --------------------------------------------------------------------------
# P8.9 — лимит сессий
# --------------------------------------------------------------------------

def test_max_active_sessions_config_exists():
    from config import MAX_ACTIVE_SESSIONS, SESSION_START_BATCH
    assert MAX_ACTIVE_SESSIONS >= 0
    assert SESSION_START_BATCH >= 1


def test_start_all_respects_max_sessions(monkeypatch, mgr):
    from config import MAX_ACTIVE_SESSIONS
    monkeypatch.setattr("utils.telethon_manager.MAX_ACTIVE_SESSIONS", 3)
    monkeypatch.setattr("utils.telethon_manager.SESSION_START_BATCH", 10)
    monkeypatch.setattr("utils.telethon_manager.SESSION_START_DELAY", 0.0)
    _fill(monkeypatch, 10)

    started = []

    async def fake_restore(uid):
        started.append(uid)
        return True

    monkeypatch.setattr(mgr, "_restore_client", fake_restore)
    asyncio.run(mgr.start_all_active())
    assert len(started) == 3, f"expected 3 clients, started {len(started)}"


def test_start_all_no_limit_when_zero(monkeypatch, mgr):
    monkeypatch.setattr("utils.telethon_manager.MAX_ACTIVE_SESSIONS", 0)
    monkeypatch.setattr("utils.telethon_manager.SESSION_START_BATCH", 100)
    monkeypatch.setattr("utils.telethon_manager.SESSION_START_DELAY", 0.0)
    _fill(monkeypatch, 7)

    started = []

    async def fake_restore(uid):
        started.append(uid)
        return True

    monkeypatch.setattr(mgr, "_restore_client", fake_restore)
    asyncio.run(mgr.start_all_active())
    assert len(started) == 7


def test_start_all_runs_in_batches(monkeypatch, mgr):
    """Волны, а не один залп: иначе 200 connect() от одного IP = FloodWait."""
    monkeypatch.setattr("utils.telethon_manager.MAX_ACTIVE_SESSIONS", 0)
    monkeypatch.setattr("utils.telethon_manager.SESSION_START_BATCH", 2)
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("utils.telethon_manager.asyncio.sleep", fake_sleep)
    _fill(monkeypatch, 6)

    async def fake_restore(uid):
        return True

    monkeypatch.setattr(mgr, "_restore_client", fake_restore)
    asyncio.run(mgr.start_all_active())
    # 6 сессий по 2 в волне = 3 волны = 2 паузы
    assert len(sleeps) == 2, f"expected 2 inter-batch pauses, got {len(sleeps)}"


def test_start_all_skips_non_active(monkeypatch, mgr):
    monkeypatch.setattr("utils.telethon_manager.SESSION_START_BATCH", 10)
    monkeypatch.setattr("utils.telethon_manager.SESSION_START_DELAY", 0.0)
    from utils import storage as ST
    ST.user_sessions.clear()
    ST.user_sessions["1"] = {"phone": "+7", "status": "active"}
    ST.user_sessions["2"] = {"phone": "+7", "status": "pending"}
    ST.user_sessions["3"] = {"phone": "+7"}  # без status

    started = []

    async def fake_restore(uid):
        started.append(uid)
        return True

    monkeypatch.setattr(mgr, "_restore_client", fake_restore)
    asyncio.run(mgr.start_all_active())
    assert started == ["1"]


# --------------------------------------------------------------------------
# P8.10 — allowlist виден в UI
# --------------------------------------------------------------------------

def test_start_py_reports_allowlist():
    """/start не должен предлагать «Включить» тому, кому запрещено."""
    import inspect

    from handlers.commands import start as start_mod

    src = inspect.getsource(start_mod)
    assert "session_allowed" in src, "/start must consult SESSION_ALLOWLIST"
    assert "may_connect" in src, "the button must be hidden, not just the text"


def test_start_kbd_hides_connect_for_blocked_user(monkeypatch):
    """Динамика: у заблокированного юзера кнопки «Включить» быть не должно."""
    from handlers.commands import start as start_mod

    kbd = start_mod._session_kb(has_session=False, may_connect=False)
    flat = [b.callback_data for row in kbd.inline_keyboard for b in row]
    assert "c1" not in flat, "blocked user must not see the connect button"
    assert "w1" in flat, "explanation button must stay"

    kbd_ok = start_mod._session_kb(has_session=False, may_connect=True)
    flat_ok = [b.callback_data for row in kbd_ok.inline_keyboard for b in row]
    assert "c1" in flat_ok


def test_start_kbd_offers_logout_when_session_on():
    from handlers.commands import start as start_mod

    kbd = start_mod._session_kb(has_session=True, may_connect=False)
    flat = [b.callback_data for row in kbd.inline_keyboard for b in row]
    assert "logout_ask" in flat
    assert "c1" not in flat


def test_status_py_reports_allowlist():
    import inspect

    from handlers.commands import status as status_mod

    src = inspect.getsource(status_mod)
    assert "session_allowed" in src, "/status must consult SESSION_ALLOWLIST"


def test_session_module_exposes_helpers():
    assert callable(S.session_allowed)
    assert callable(S.allowlist_active)
