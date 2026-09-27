"""Покрытие Telethon-хендлеров: dispatch + watch/del/save/dm/quote/vgf/nya/tagall/admins/pin."""

import asyncio
from types import SimpleNamespace

import pytest

from utils.telethon_manager import telethon_manager
from utils import storage

_chat_seq = [-9100]


def _next_chat():
    _chat_seq[0] -= 1
    return _chat_seq[0]


class StubClient:
    def __init__(self, history=None, fail_call=None):
        self.me = SimpleNamespace(id=111)
        self.history = history or []
        self.deleted = []
        self.forwarded = []
        self.sent = []
        self.fail_call = fail_call

    async def __call__(self, *args, **kwargs):
        if self.fail_call:
            raise self.fail_call
        return SimpleNamespace(participants=[])

    async def get_me(self):
        return self.me

    async def get_entity(self, *args, **kwargs):
        raise ValueError("no such entity")

    async def iter_messages(self, chat_id, **kwargs):
        for m in self.history:
            yield m

    async def iter_participants(self, chat_id, **kwargs):
        return
        yield  # pragma: no cover - make it an async generator

    async def delete_messages(self, chat_id, ids):
        self.deleted.extend(ids)

    async def forward_messages(self, *args):
        self.forwarded.append(args)

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def is_connected(self):
        return False


class StubEvent:
    def __init__(self, text, uid="u-th", chat_id=None, mid=1, private=False, bot=False, reply=None):
        self.raw_text = text
        self.chat_id = chat_id if chat_id is not None else _next_chat()
        self.id = mid
        self.is_private = private
        self.message = None
        self.chat = SimpleNamespace(title="TestChat", first_name=None, bot=bot, megagroup=False, broadcast=False)
        self.client = StubClient()
        self.reply = reply
        self.edits = []
        self.deleted = False
        self.date = None

    async def edit(self, text, **kwargs):
        self.edits.append(text)

    async def respond(self, *args, **kwargs):
        self.edits.append(args[0] if args else "")

    async def delete(self):
        self.deleted = True

    async def get_sender(self):
        return SimpleNamespace(id=111, username="tester", first_name="Test", last_name=None,
                               bot=False, premium=False, is_premium=False)

    async def get_chat(self):
        return self.chat

    async def get_reply_message(self):
        return self.reply


def _msg(mid, text="hi"):
    return SimpleNamespace(id=mid, raw_text=text, message=text, sender_id=111,
                           sender=SimpleNamespace(id=111), date=None)


def run_out(uid, event):
    return asyncio.run(telethon_manager._handle_outgoing(uid, event))


@pytest.fixture(autouse=True)
def _no_disk_writes(monkeypatch):
    monkeypatch.setattr(storage, "save_watched", lambda: None)
    monkeypatch.setattr(storage, "save_user_tz", lambda: None)
    monkeypatch.setattr(storage, "save_nya_chats", lambda: None)
    monkeypatch.setattr(storage, "save_auto_tr_chats", lambda: None)
    yield
    for d in ("watched_chats", "nya_chats", "user_timezones"):
        getattr(storage, d).pop("u-th", None)
        getattr(storage, d).pop("u-th2", None)


def test_dispatch_empty_and_unknown():
    ev = StubEvent("")
    run_out("u-th", ev)
    assert ev.edits == []
    ev2 = StubEvent(".пинк")
    run_out("u-th", ev2)
    assert ev2.edits and ".ping" in ev2.edits[-1]


def test_dispatch_private_bot_skipped():
    ev = StubEvent(".ping", private=True, bot=True)
    run_out("u-th", ev)
    assert ev.edits == []


def test_dispatch_ping_uuid_coin_help_id_calc():
    ev = StubEvent(".ping")
    run_out("u-th", ev)
    assert any("Ping" in e for e in ev.edits)

    ev = StubEvent(".uuid 3")
    run_out("u-th", ev)
    assert ev.edits[-1].count("<code>") == 3

    ev = StubEvent(".coin")
    run_out("u-th", ev)
    assert "Орёл" in ev.edits[-1] or "Решка" in ev.edits[-1]

    ev = StubEvent(".help")
    run_out("u-th", ev)
    assert ".ping" in ev.edits[-1]

    ev = StubEvent(".id")
    run_out("u-th", ev)
    assert "111" in ev.edits[-1]

    ev = StubEvent(".calc 2+2")
    run_out("u-th", ev)
    assert "<b>4</b>" in ev.edits[-1]


def test_dispatch_timezone_and_b64():
    ev = StubEvent(".timezone +3")
    run_out("u-th", ev)
    assert "UTC+3" in ev.edits[-1] or "+3" in ev.edits[-1]

    ev = StubEvent(".b64 hi")
    run_out("u-th", ev)
    assert "aGk=" in ev.edits[-1]


def test_watch_add_unwatch_watched():
    ev = StubEvent(".watch")
    run_out("u-th", ev)
    assert "теперь отслеживается" in ev.edits[-1]
    assert "Первый чат" in ev.edits[-1]

    ev = StubEvent(".watch")
    ev.chat_id, ev.id = _next_chat(), 2
    run_out("u-th", ev)

    ev = StubEvent(".watched")
    run_out("u-th", ev)
    assert "Вотч" in ev.edits[-1] or "WATCHED" in ev.edits[-1].upper() or "топик" in ev.edits[-1] or "весь чат" in ev.edits[-1]

    first_chat = storage.watched_chats["u-th"][0][0]
    ev = StubEvent(".unwatch", chat_id=first_chat, mid=9)
    run_out("u-th", ev)
    assert "отключено" in ev.edits[-1]


def test_del_deletes_own_messages():
    history = [_msg(1, "cmd"), _msg(5, "a"), _msg(6, "b")]
    ev = StubEvent(".del", mid=1)
    ev.client.history = history
    run_out("u-th", ev)
    assert 1 in ev.client.deleted
    assert 5 in ev.client.deleted


def test_save_dm_quote_vgf_need_reply():
    for text, marker in ((".save", "Ответь"), (".влс", "Ответь"), (".цитата", "цитату"), (".вгф", "фото или видео")):
        ev = StubEvent(text)
        run_out("u-th", ev)
        assert marker in ev.edits[-1], text


def test_save_forwards_with_reply():
    reply = _msg(7, "keep me")
    ev = StubEvent(".save", reply=reply)
    run_out("u-th", ev)
    assert ev.client.forwarded
    assert "Избранное" in ev.edits[-1]


def test_nya_toggle_and_list():
    ev = StubEvent(".ня")
    first_chat = ev.chat_id
    run_out("u-th", ev)
    assert "ВКЛЮЧЁН" in ev.edits[-1] or "включ" in ev.edits[-1].lower()

    ev = StubEvent(".ня список")
    run_out("u-th", ev)
    assert str(first_chat) in ev.edits[-1]

    ev = StubEvent(".ня стоп", chat_id=first_chat, mid=5)
    run_out("u-th", ev)
    assert "выключен" in ev.edits[-1].lower()


def test_tagall_admins_only_groups():
    ev = StubEvent(".tagall")
    run_out("u-th", ev)
    assert "групп" in ev.edits[-1]

    ev = StubEvent(".admins")
    run_out("u-th", ev)
    assert "групп" in ev.edits[-1]


def test_pin_error_card():
    ev = StubEvent(".pin")
    ev.client.fail_call = RuntimeError("nope")
    run_out("u-th", ev)
    assert "RuntimeError" in ev.edits[-1]
