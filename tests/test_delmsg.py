"""P1.4 — `.del` не должен бросать необработанное исключение.

Баг: команда добавляла `event.id` в список удаления, удаляла его, а затем
безусловно вызывала `await event.delete()` — второе удаление того же id кидало
MessageIdInvalidError наружу, и `outgoing_handler` логировал
`logger.exception` на КАЖДЫЙ `.del`.
"""

import asyncio

import pytest
from telethon.errors import FloodWaitError, MessageDeleteForbiddenError

from handlers.commands.delmsg import handle


class FakeMe:
    def __init__(self, uid=1):
        self.id = uid


class FakeEvent:
    def __init__(self, client, text=".del", event_id=100, chat_id=-100):
        self.client = client
        self.raw_text = text
        self.id = event_id
        self.chat_id = chat_id
        self.edits = []
        self.deleted = 0

    async def edit(self, text, **kwargs):
        self.edits.append(text)

    async def delete(self):
        self.deleted += 1


class FakeClient:
    """Минимальный Telethon-клиент: get_me + iter_messages + delete_messages."""

    def __init__(self, history=(), delete_error=None, me_error=None):
        self._history = list(history)
        self._delete_error = delete_error
        self._me_error = me_error
        self.deleted_batches = []

    async def get_me(self):
        if self._me_error:
            raise self._me_error
        return FakeMe()

    async def iter_messages(self, chat_id, from_user=None, limit=0):
        for mid in self._history:
            yield type("M", (), {"id": mid})()

    async def delete_messages(self, chat_id, ids):
        if self._delete_error:
            raise self._delete_error
        self.deleted_batches.append(list(ids))


def _event(**kw):
    client = FakeClient(history=kw.pop("history", (200, 201, 202)))
    return FakeEvent(client, **kw), client


def test_del_does_not_delete_event_twice():
    """Ядро фикса: после delete_messages НЕТ повторного event.delete()."""
    event, client = _event()
    asyncio.run(handle("1", event))
    assert event.deleted == 0, "event.delete() must not be called after delete_messages"
    assert client.deleted_batches, "delete_messages was never called"
    assert client.deleted_batches[0][0] == event.id


def test_del_includes_command_and_n_previous():
    event, client = _event(history=(200, 201, 202, 203))
    asyncio.run(handle("1", event))
    deleted = client.deleted_batches[0]
    assert deleted[0] == 100, "command itself must be first"
    # .del без аргументов => N=1 => всего 2 сообщения
    assert len(deleted) == 2, deleted


def test_del_n_argument_extends_count():
    event, client = _event(text=".del 3", history=(200, 201, 202, 203, 204))
    asyncio.run(handle("1", event))
    deleted = client.deleted_batches[0]
    assert len(deleted) == 4, deleted  # N=3 + команда


def test_del_skips_own_id_from_history():
    event, client = _event(history=(200, 100, 201))
    asyncio.run(handle("1", event))
    deleted = client.deleted_batches[0]
    assert deleted.count(100) == 1, f"command id duplicated: {deleted}"


@pytest.mark.parametrize("n_arg,expected", [("0", 2), ("1", 2), ("100", 101)])
def test_del_clamps_n(n_arg, expected):
    event, client = _event(text=f".del {n_arg}", history=tuple(range(300, 400)))
    asyncio.run(handle("1", event))
    assert len(client.deleted_batches[0]) == min(expected, 101)


def test_delete_failure_does_not_raise():
    """Ошибка delete_messages обрабатывается, а не пробрасывается."""
    event, client = _event()
    client._delete_error = RuntimeError("boom")
    asyncio.run(handle("1", event))  # не должно бросить
    assert event.deleted == 0
    assert event.edits, "expected an error card to be shown"


def test_forbidden_error_is_reported():
    event, client = _event()
    client._delete_error = MessageDeleteForbiddenError(request=None)
    asyncio.run(handle("1", event))
    assert event.edits
    assert event.deleted == 0


def test_floodwait_error_is_reported():
    event, client = _event()

    class FW(FloodWaitError):
        def __init__(self):
            super().__init__(request=None)
            self.seconds = 42

    client._delete_error = FW()
    asyncio.run(handle("1", event))
    assert event.edits
    assert "42" in event.edits[0], event.edits[0]


def test_get_me_failure_is_reported_not_raised():
    event, client = _event()
    client._me_error = RuntimeError("no me")
    asyncio.run(handle("1", event))
    assert event.edits
    assert event.deleted == 0


def test_safe_edit_swallows_missing_message():
    """_safe_edit переживает MessageIdInvalidError (сообщение уже удалено)."""
    from handlers.commands.delmsg import _safe_edit

    class GoneEvent:
        async def edit(self, text, **kwargs):
            raise RuntimeError("MessageIdInvalidError")

    asyncio.run(_safe_edit(GoneEvent(), "text"))  # не должно бросить
