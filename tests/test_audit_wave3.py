"""Тесты фиксов аудита, волна 3: Telethon-ядро."""

import asyncio
from types import SimpleNamespace

import utils.telethon_manager as tm
from utils.telethon_manager import (
    TelethonManager,
    thread_id_of,
    telethon_reply_to,
    parse_dot_targets,
    _topic_name_async,
    _esc,
)


def _reply_to(forum_topic=False, top=None, msg=None):
    return SimpleNamespace(forum_topic=forum_topic, reply_to_top_id=top, reply_to_msg_id=msg)


def _event(**kw):
    base = dict(
        out=False, photo=None, video=None, media=None,
        chat_id=-100, id=1, message=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_thread_id_of_forum_top_and_fallback():
    assert thread_id_of(_event(message=SimpleNamespace(reply_to=_reply_to(True, top=5, msg=7)))) == 5
    assert thread_id_of(_event(message=SimpleNamespace(reply_to=_reply_to(True, top=None, msg=7)))) == 7
    assert thread_id_of(_event(message=SimpleNamespace(reply_to=_reply_to(False, top=9, msg=9)))) == 9
    assert thread_id_of(_event(message=None)) == 0
    assert thread_id_of(_event(message=SimpleNamespace(reply_to=None))) == 0


def test_telethon_reply_to_prefers_top():
    assert telethon_reply_to(_event(message=SimpleNamespace(reply_to=_reply_to(False, top=9, msg=3)))) == 9
    assert telethon_reply_to(_event(message=SimpleNamespace(reply_to=_reply_to(False, top=None, msg=3)))) == 3
    assert telethon_reply_to(_event(message=None)) is None


def test_parse_dot_targets_dedup_limit():
    assert parse_dot_targets("@a @A @b", limit=5) == ["a", "b"]
    assert parse_dot_targets("@a,@b @c", limit=2) == ["a", "b"]
    assert parse_dot_targets("", limit=5) == []


def test_topic_name_rejects_foreign_id():
    async def go():
        async def fake_client(req):
            return SimpleNamespace(topics=[SimpleNamespace(id=999, title="Чужой")])

        assert await _topic_name_async(fake_client, -100, 5) == ""

        async def fake_client2(req):
            return SimpleNamespace(topics=[SimpleNamespace(id=5, title="Наш")])

        assert await _topic_name_async(fake_client2, -100, 5) == "Наш"
        assert await _topic_name_async(None, -100, 0) == ""

    asyncio.run(go())


def test_incoming_ignores_own_outgoing():
    mgr = TelethonManager()
    calls = []

    async def fake_index(uid, event):
        calls.append(event)

    mgr._knowledge_collector = SimpleNamespace(
        index_live=fake_index, stop=lambda uid: asyncio.sleep(0),
    )
    ev = _event(out=True)
    asyncio.run(mgr._handle_incoming("u3", ev))
    assert calls == []


def test_download_view_once_cleans_partial_and_names_chat(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(tm, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(tm, "TELETHON_RESOLVE_TIMEOUT", 5)

    class Doc:
        mime_type = "image/jpeg"

    class Media:
        pass

    from telethon.tl.types import MessageMediaPhoto

    mgr = TelethonManager()

    class Client:
        async def get_messages(self, chat_id, ids):
            return SimpleNamespace(media=MessageMediaPhoto())

        async def download_media(self, msg, file):
            with open(file, "wb") as f:
                f.write(b"partial")
            raise RuntimeError("boom")

    mgr._clients["u3dl"] = Client()
    try:
        assert asyncio.run(mgr.download_view_once("u3dl", -100, 7)) is None
        assert list(tmp_path.iterdir()) == []
    finally:
        mgr._clients.pop("u3dl", None)


def test_resolve_entity_has_timeout(monkeypatch):
    monkeypatch.setattr(tm, "TELETHON_RESOLVE_TIMEOUT", 0.05)
    mgr = TelethonManager()

    class Client:
        async def get_entity(self, target):
            await asyncio.sleep(10)

    mgr._clients["u3r"] = Client()
    try:
        assert asyncio.run(mgr.resolve_entity("u3r", "@x")) is None
    finally:
        mgr._clients.pop("u3r", None)


def test_anim_keeps_command_when_respond_fails():
    import pytest

    mgr = TelethonManager()

    class Event:
        chat_id = -100
        id = 9
        message = None
        deleted = False

        async def respond(self, *args, **kwargs):
            raise RuntimeError("send fail")

        async def delete(self):
            self.deleted = True

    ev = Event()
    with pytest.raises(RuntimeError):
        asyncio.run(mgr._handle_anim("nouser", ev, kind="love"))
    assert ev.deleted is False


def test_anim_deletes_command_on_success():
    mgr = TelethonManager()

    class Msg:
        async def edit(self, *args, **kwargs):
            pass

    class Event:
        chat_id = -100
        id = 9
        message = None
        deleted = False

        async def respond(self, *args, **kwargs):
            return Msg()

        async def delete(self):
            self.deleted = True

    ev = Event()
    asyncio.run(mgr._handle_anim("nouser", ev, kind="love"))
    assert ev.deleted is True


def test_who_summary_escapes_user_input():
    mgr = TelethonManager()

    class Client:
        async def get_entity(self, target):
            raise ValueError("<bad>")

    mgr._clients["u3w"] = Client()
    edits = []

    class Event:
        chat_id = -100
        id = 11

        async def get_sender(self):
            return None

        async def edit(self, text, **kwargs):
            edits.append(text)

    try:
        asyncio.run(mgr._who_with_args("u3w", Event(), "<bad>"))
    finally:
        mgr._clients.pop("u3w", None)
    assert edits
    assert "&lt;bad&gt;" in edits[-1]
    assert "<bad>" not in edits[-1]


def test_health_loop_survives_iteration_error(monkeypatch):
    monkeypatch.setattr(tm, "TELETHON_HEALTH_INTERVAL", 0.01)
    mgr = TelethonManager()
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {}

    async def go():
        mgr.check_clients_health = flaky
        task = asyncio.create_task(mgr._health_check_loop())
        await asyncio.sleep(0.06)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return len(calls) >= 2

    assert asyncio.run(go()) is True


def test_logout_cancels_and_joins_task(monkeypatch):
    import utils.knowledge_db as kdb

    monkeypatch.setattr(kdb, "clear_owner", lambda uid: None)
    mgr = TelethonManager()
    cancelled = []

    async def stuck():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def go():
        mgr._knowledge_collector = SimpleNamespace(stop=lambda uid: asyncio.sleep(0))

        class Client:
            async def log_out(self):
                return True

            async def disconnect(self):
                pass

        mgr._clients["u3lo"] = Client()
        task = asyncio.create_task(stuck())
        mgr._tasks["u3lo"] = task
        await mgr.logout("u3lo")
        return task.done()

    assert asyncio.run(go()) is True
    assert cancelled == [True]
    assert "u3lo" not in mgr._clients
    assert "u3lo" not in mgr._stopping
