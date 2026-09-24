"""Тесты фиксов аудита, волна 2: session auth flow."""

import asyncio
import time
from types import SimpleNamespace

from telethon.errors import FloodWaitError, PasswordHashInvalidError

import handlers.session as session
from handlers.session import auth_states


class StubMsg:
    def __init__(self, text=None, chat_type="private", uid="u2"):
        self.text = text
        self.chat = SimpleNamespace(type=chat_type)
        self.from_user = SimpleNamespace(id=uid)
        self.answers = []
        self.replies = []
        self.edits = []

    async def answer(self, t, **kwargs):
        self.answers.append(t)

    async def reply(self, t, **kwargs):
        self.replies.append(t)

    async def edit_text(self, t, **kwargs):
        self.edits.append(t)

    async def edit_reply_markup(self, **kwargs):
        pass


class StubCb:
    def __init__(self, data, uid="u2", msg=None):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.message = msg or StubMsg(uid=uid)
        self.answers = []

    async def answer(self, t=None, **kwargs):
        self.answers.append(t)


class StubClient:
    def __init__(self, exc=None):
        self.exc = exc
        self.disconnected = False

    async def sign_in(self, *args, **kwargs):
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(id=1)

    async def disconnect(self):
        self.disconnected = True


def _clean(uid):
    auth_states.pop(uid, None)
    session._AUTH_ATTEMPTS.pop(uid, None)


def test_twofa_filter_private_only():
    uid = "u2f"
    auth_states[uid] = {"step": "2fa"}
    try:
        assert session._is_twofa_message(StubMsg("pw", "private", uid)) is True
        assert session._is_twofa_message(StubMsg("pw", "group", uid)) is False
        assert session._is_twofa_message(StubMsg(".ping", "private", uid)) is True
        assert session._is_twofa_message(StubMsg("pw", "private", "other")) is False
    finally:
        _clean(uid)


def test_phone_kb_rejects_forged_multichar():
    uid = "u2p"
    auth_states[uid] = {"step": "phone", "s": "79"}
    try:
        asyncio.run(session.phone_kb(StubCb("p:abcd", uid)))
        assert auth_states[uid]["s"] == "79"
        asyncio.run(session.phone_kb(StubCb("p:5", uid)))
        assert auth_states[uid]["s"] == "795"
    finally:
        _clean(uid)


def test_code_kb_rejects_forged_multichar():
    uid = "u2c"
    auth_states[uid] = {"step": "code", "s": "12"}
    try:
        asyncio.run(session.code_kb(StubCb("d:xyz", uid)))
        assert auth_states[uid]["s"] == "12"
    finally:
        _clean(uid)


def test_code_floodwait_keeps_state_for_retry():
    uid = "u2fw"
    client = StubClient(exc=FloodWaitError(None, capture=5))
    auth_states[uid] = {
        "step": "code", "s": "12345", "phone": "+7000",
        "client": client, "hash": "h" * 10, "_ts": time.time(),
    }
    try:
        cb = StubCb("dgo", uid)
        asyncio.run(session.code_kb(cb))
        assert auth_states[uid]["step"] == "code"
        assert auth_states[uid]["client"] is client
        assert client.disconnected is False
        assert any("5" in (a or "") for a in cb.answers)
    finally:
        _clean(uid)


def test_twofa_wrong_password_keeps_state_for_retry():
    uid = "u2wp"
    client = StubClient(exc=PasswordHashInvalidError(None))
    auth_states[uid] = {
        "step": "2fa", "phone": "+7000", "client": client, "_ts": time.time(),
    }
    try:
        msg = StubMsg("wrong", "private", uid)
        asyncio.run(session.twofa_handler(msg))
        assert auth_states[uid]["step"] == "2fa"
        assert client.disconnected is False
        assert any("ещё раз" in r for r in msg.replies)
    finally:
        _clean(uid)


def test_auth_attempts_dict_does_not_grow_unbounded():
    now = time.monotonic()
    for i in range(2500):
        session._AUTH_ATTEMPTS[f"spam-{i}"].append(now - 10_000)
    try:
        assert session._allow_auth_attempt("fresh-uid") is True
        assert len(session._AUTH_ATTEMPTS) <= 1001
    finally:
        for i in range(2500):
            session._AUTH_ATTEMPTS.pop(f"spam-{i}", None)
        session._AUTH_ATTEMPTS.pop("fresh-uid", None)
