"""P2.1/P2.4/P2.5 — гейт allowlist, удаление 2FA-пароля, честный status.

- P2.1: `connect_cb` пускал любого, кто нашёл бота, в подключение своего
  аккаунта (и к общему LLM-ключу).
- P2.4: 2FA-пароль оставался в истории чата — `message.delete()` не вызывался.
- P2.5: `_finish` писал `status:"active"` ДО `start_client`, поэтому при
  неудаче юзер видел «Готово» / «Включено», а команды не работали.
"""

import asyncio

import pytest

from handlers import session as S
from utils.premium import _PREMIUM_CACHE


@pytest.fixture(autouse=True)
def _clean_state():
    S.auth_states.clear()
    _PREMIUM_CACHE.clear()
    yield
    S.auth_states.clear()
    _PREMIUM_CACHE.clear()


class FakeCallback:
    def __init__(self, uid):
        self.from_user = type("U", (), {"id": uid})()
        self.answers = []
        self.alerts = []
        self.message = FakeCallbackMessage()

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)
        if show_alert:
            self.alerts.append(text)


class FakeCallbackMessage:
    def __init__(self):
        self.edits = []
        self.answers = []
        self.bot = type("B", (), {"send_message": None})()

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class FakeMessage:
    def __init__(self, uid, text="secret-password"):
        self.from_user = type("U", (), {"id": uid})()
        self.chat = type("C", (), {"type": "private"})()
        self.text = text
        self.replies = []
        self.deleted = 0

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    async def delete(self):
        self.deleted += 1


# --------------------------------------------------------------------------
# P2.1 — allowlist
# --------------------------------------------------------------------------

def test_empty_allowlist_is_open_mode(monkeypatch):
    monkeypatch.setattr(S, "SESSION_ALLOWLIST", set())
    assert S.session_allowed("123") is True
    assert S.allowlist_active() is False


def test_allowlist_admits_only_listed(monkeypatch):
    monkeypatch.setattr(S, "SESSION_ALLOWLIST", {"111", "222"})
    assert S.allowlist_active() is True
    assert S.session_allowed("111") is True
    assert S.session_allowed("222") is True
    assert S.session_allowed("333") is False


def test_connect_cb_rejects_not_allowed(monkeypatch):
    monkeypatch.setattr(S, "SESSION_ALLOWLIST", {"111"})
    cb = FakeCallback(333)
    asyncio.run(S.connect_cb(cb))
    assert cb.alerts, "expected a visible refusal"
    assert "333" not in S.auth_states, "auth state must not be created for a rejected user"
    assert not cb.message.edits, "the phone prompt must not be shown"


def test_connect_cb_admits_allowed(monkeypatch):
    monkeypatch.setattr(S, "SESSION_ALLOWLIST", {"111"})
    cb = FakeCallback(111)
    asyncio.run(S.connect_cb(cb))
    assert "111" in S.auth_states
    assert S.auth_states["111"]["step"] == "phone"
    assert cb.message.edits, "the phone prompt must be shown"


def test_start_auth_rechecks_allowlist(monkeypatch):
    """Повторная проверка на случай смены allowlist между колбэками."""
    monkeypatch.setattr(S, "SESSION_ALLOWLIST", {"111"})
    S.auth_states["111"] = {"step": "phone", "s": "", "_ts": 0}
    msg = FakeCallbackMessage()
    asyncio.run(S._start_auth(msg, "111", "+79001234567"))
    assert "111" not in S.auth_states, "state must be dropped on re-check failure"
    assert msg.answers, "user must be told"


def test_allowlist_parsing_from_env(monkeypatch):
    """Парсинг SESSION_ALLOWLIST: запятые, точки с запятой, пробелы."""
    import importlib

    import config

    monkeypatch.setenv("SESSION_ALLOWLIST", " 111,222 ; 333 ")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.SESSION_ALLOWLIST == {"111", "222", "333"}
    finally:
        monkeypatch.delenv("SESSION_ALLOWLIST", raising=False)
        importlib.reload(config)


def test_empty_allowlist_parses_to_empty_set(monkeypatch):
    import importlib

    import config

    monkeypatch.setenv("SESSION_ALLOWLIST", "")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.SESSION_ALLOWLIST == set()
    finally:
        monkeypatch.delenv("SESSION_ALLOWLIST", raising=False)
        importlib.reload(config)


# --------------------------------------------------------------------------
# P2.4 — 2FA пароль удаляется
# --------------------------------------------------------------------------

def test_erase_password_deletes_message():
    msg = FakeMessage(111)
    asyncio.run(S._erase_password(msg))
    assert msg.deleted == 1


def test_erase_password_swallows_failure():
    class Gone:
        async def delete(self):
            raise RuntimeError("MessageIdInvalidError")

    asyncio.run(S._erase_password(Gone()))  # не должно бросить


def test_twofa_deletes_password_before_signin(monkeypatch):
    """Пароль удаляется ДО sign_in, даже если sign_in упадёт."""
    signin_calls = []

    class FakeClient:
        async def sign_in(self, password=None, **kw):
            signin_calls.append(password)
            raise S.PasswordHashInvalidError(request=None)

        async def disconnect(self):
            pass

    uid = "111"
    S.auth_states[uid] = {
        "step": "2fa", "phone": "+7900", "client": FakeClient(), "hash": "h", "s": "", "_ts": 0,
    }
    monkeypatch.setattr(S, "_allow_auth_attempt", lambda u: True)
    msg = FakeMessage(111, "wrong-pass")
    asyncio.run(S.twofa_handler(msg))
    assert msg.deleted == 1, "password message must be deleted"
    assert signin_calls == ["wrong-pass"]


def test_twofa_deletes_password_on_rate_limit(monkeypatch):
    class FakeClient:
        async def sign_in(self, password=None, **kw):
            raise AssertionError("sign_in must not be called")

        async def disconnect(self):
            pass

    uid = "111"
    S.auth_states[uid] = {
        "step": "2fa", "phone": "+7900", "client": FakeClient(), "hash": "h", "s": "", "_ts": 0,
    }
    monkeypatch.setattr(S, "_allow_auth_attempt", lambda u: False)
    msg = FakeMessage(111, "pass")
    asyncio.run(S.twofa_handler(msg))
    assert msg.deleted == 1, "password must be erased even when rate-limited"


# --------------------------------------------------------------------------
# P2.5 — status:"active" только после успешного старта
# --------------------------------------------------------------------------

class FakeAuthClient:
    def __init__(self):
        self.disconnected = False

    async def get_me(self):
        return type("Me", (), {"id": 111})()

    async def disconnect(self):
        self.disconnected = True


def test_finish_persists_only_on_success(monkeypatch):
    from utils.storage import user_sessions

    user_sessions.pop("111", None)
    monkeypatch.setattr(S.telethon_manager, "start_client", lambda uid: asyncio.sleep(0, result=True))
    _PREMIUM_CACHE["111"] = (False, 1.0)

    ok = asyncio.run(S._finish("111", FakeAuthClient(), "+7900", twofa=True))
    assert ok is True
    assert user_sessions["111"]["status"] == "active"
    assert "111" not in _PREMIUM_CACHE, "premium cache must be invalidated after connect"
    user_sessions.pop("111", None)


def test_finish_does_not_persist_active_on_failure(monkeypatch):
    """status обязан быть НЕ active, иначе /start покажет «Включено»."""
    from utils.storage import user_sessions

    user_sessions.pop("222", None)
    monkeypatch.setattr(S.telethon_manager, "start_client", lambda uid: asyncio.sleep(0, result=False))

    ok = asyncio.run(S._finish("222", FakeAuthClient(), "+7900", twofa=False))
    assert ok is False
    rec = user_sessions.get("222")
    assert rec is not None, "record must exist to protect the .session file"
    assert rec["status"] != "active", (
        "a session whose client failed to start must NOT be persisted as active"
    )


def test_failed_finish_protects_session_file_from_orphan_cleanup(monkeypatch, tmp_path):
    """P8.6: sign_in уже создал .session. Без записи в user_sessions
    cleanup_orphan_sessions() удалил бы его на старте — юзеру пришлось бы
    проходить аутентификацию заново из-за сетевого сбоя."""
    import hashlib

    from utils import storage as ST
    from utils.telethon_manager import cleanup_orphan_sessions
    import utils.telethon_manager as TLM

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    h = hashlib.sha256("777".encode()).hexdigest()[:16]
    path = str(sessions_dir / h)
    monkeypatch.setattr(S, "session_path", lambda uid: path)
    monkeypatch.setattr(TLM, "session_path", lambda uid: path)
    monkeypatch.setattr(TLM, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(TLM, "save_user_sessions", lambda: None)
    monkeypatch.setattr(S.telethon_manager, "start_client", lambda uid: asyncio.sleep(0, result=False))

    ok = asyncio.run(S._finish("777", FakeAuthClient(), "+7900", twofa=False))
    assert ok is False

    # эмулируем то, что делает бот при старте
    (sessions_dir / f"{h}.session").write_bytes(b"fake")
    orphan = sessions_dir / "ffffffffffffffff.session"
    orphan.write_bytes(b"fake")

    # ВАЖНО: через monkeypatch — прямое присваивание TLM.user_sessions
    # утекало бы в остальные тесты (модуль импортирует словарь по имени).
    monkeypatch.setattr(TLM, "user_sessions", dict(ST.user_sessions))
    cleanup_orphan_sessions()
    assert (sessions_dir / f"{h}.session").exists(), (
        "authorized .session must NOT be deleted as an orphan"
    )
    assert not orphan.exists(), "genuine orphan must be removed"
    ST.user_sessions.pop("777", None)


def test_finish_clears_auth_state_on_failure(monkeypatch):
    from utils.storage import user_sessions

    user_sessions.pop("333", None)
    S.auth_states["333"] = {"step": "verifying"}
    monkeypatch.setattr(S.telethon_manager, "start_client", lambda uid: asyncio.sleep(0, result=False))
    asyncio.run(S._finish("333", FakeAuthClient(), "+7900", twofa=False))
    assert "333" not in S.auth_states


def test_session_exists_false_after_failed_finish(monkeypatch):
    """Сквозная проверка: session_exists обязан быть False после неудачи."""
    from utils.storage import save_user_sessions, session_exists, user_sessions

    user_sessions.pop("444", None)
    save_user_sessions()
    monkeypatch.setattr(S.telethon_manager, "start_client", lambda uid: asyncio.sleep(0, result=False))
    asyncio.run(S._finish("444", FakeAuthClient(), "+7900", twofa=False))
    assert session_exists("444") is False
