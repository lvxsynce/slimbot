"""Тесты команд `.шаб` / `.+шаб` / `.-шаб` (Telethon-слой).

Классификация типов и хранилище покрыты в tests/test_templates.py; здесь —
диспетчер команд, лимиты, пагинация и корректность вызовов send_file/send_message.
"""

import asyncio
from types import SimpleNamespace

import pytest
from telethon.tl.types import (
    Document, DocumentAttributeAudio, DocumentAttributeVideo, Photo, PhotoSize,
)

from handlers.commands import template as T
from utils import media_kind as mk
from utils import storage


def _ns(**kw):
    return SimpleNamespace(**kw)


def _doc(mime="", size=0, attributes=None):
    return Document(id=1, access_hash=2, file_reference=b"", date=None,
                    mime_type=mime, size=size, dc_id=2,
                    attributes=list(attributes or []))


def _reply(text="", document=None):
    msg = _ns(message=text, raw_text=text, caption=None, document=document)
    return msg


class FakeClient:
    """Клиент-заглушка: пишет вызовы в .sent вместо реальной отправки.

    `reply_to` прогоняется через НАСТОЯЩИЙ telethon.utils.get_message_id —
    именно он падает с «Invalid message type» на мусоре в reply_to. Без этой
    проверки заглушка молча принимала бы баг «передали саму функцию
    telethon_reply_to вместо её вызова» — ровно тот баг, что уехал в прод.
    """

    def __init__(self):
        self.sent = []

    @staticmethod
    def _check(reply_to):
        from telethon.utils import get_message_id
        if reply_to is not None:
            get_message_id(reply_to)  # TypeError на мусоре в reply_to

    async def send_message(self, chat, message=None, file=None, **kw):
        self._check(kw.get("reply_to"))
        self.sent.append({"call": "message", "text": message, "file": file,
                          **kw})

    async def send_file(self, chat, file=None, **kw):
        self._check(kw.get("reply_to"))
        data = file.read() if hasattr(file, "read") else b""
        self.sent.append({"call": "file", "data": data,
                          "name": getattr(file, "name", None), **kw})

    async def get_me(self):
        return _ns(id=1, premium=True)


class FakeEvent:
    def __init__(self, text="", client=None, reply=None, chat_id=42):
        self.raw_text = text
        self.client = client or FakeClient()
        self.chat_id = chat_id
        self.message = _ns(reply_to=None)
        self._reply = reply
        self.edited = []
        self.deleted = False

    async def get_reply_message(self):
        return self._reply

    async def edit(self, text, parse_mode=None, **kw):
        self.edited.append(text)

    async def delete(self):
        self.deleted = True

    @property
    def out(self) -> str:
        return self.edited[-1] if self.edited else ""


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Изолированное хранилище + отключённая Premium-проба (сеть не нужна)."""
    monkeypatch.setattr(storage, "TEMPLATES_FILE", tmp_path / "templates.json")
    monkeypatch.setattr(storage, "TEMPLATES_DIR", tmp_path / "templates")
    monkeypatch.setattr(storage, "templates", {})

    async def _no_premium(client=None):
        return False

    monkeypatch.setattr(T, "_premium", _no_premium)
    return storage


# --------------------------------------------------------------------------
# диспетчер
# --------------------------------------------------------------------------

def test_dispatch_cmds_match_template_module():
    """Литерал в telethon_manager не должен разъезжаться с модулем."""
    from utils.telethon_manager import _TEMPLATE_CMDS
    assert set(_TEMPLATE_CMDS) == set(T.ALL_CMDS)
    assert {".шаб", ".+шаб", ".-шаб"} <= set(T.ALL_CMDS)


def test_send_gif_template_end_to_end(env):
    """Регрессия из прод-бага №1: `.+шаб 123` на GIF проходил, а `.шаб 123`
    падал с «TypeError: Invalid message type: <class 'function'>» — в
    `_cmd_send` передавалась САМА функция telethon_reply_to, а её результат.
    """
    from telethon.tl.types import DocumentAttributeAnimated
    doc = _doc("video/mp4", 70 * 1024, [DocumentAttributeAnimated()])

    async def _dl(file=None):
        return b"GIF89a" + b"\x00" * 1024

    reply = _reply("", doc)
    reply.download_media = _dl
    ev = FakeEvent(".+шаб 123", reply=reply)
    _run(T.handle("u1", ev))
    assert "[+] Шаблон сохранён" in ev.out

    ev2 = FakeEvent(".шаб 123")
    _run(T.handle("u1", ev2))
    assert not ev2.edited, ev2.out          # не должно быть карточки с ошибкой
    assert len(ev2.client.sent) == 1
    sent = ev2.client.sent[0]
    assert sent["call"] == "file"
    assert any(type(a).__name__ == "DocumentAttributeAnimated"
               for a in sent["attributes"])
    assert ev2.deleted is True


def test_send_reply_to_is_resolved_value_not_callable(env):
    """reply_to обязан быть int/None, а не объектом-функцией."""
    storage.put_template("u1", "ш", {"kind": "text", "text": "x"})
    ev = FakeEvent(".шаб ш")
    _run(T.handle("u1", ev))
    reply_to = ev.client.sent[0]["reply_to"]
    assert reply_to is None or isinstance(reply_to, int)
    assert not callable(reply_to)


def test_send_reply_to_uses_topic_id(env):
    """В форуме ответ должен уехать в тот же топик."""
    storage.put_template("u1", "ш", {"kind": "text", "text": "x"})
    ev = FakeEvent(".шаб ш")
    ev.message = _ns(reply_to=_ns(reply_to_top_id=555, reply_to_msg_id=42))
    _run(T.handle("u1", ev))
    assert ev.client.sent[0]["reply_to"] == 555


def test_send_reports_error_text_not_only_class(env):
    """Пользователь должен видеть, ЧТО сломалось, а не только имя класса."""
    blob = storage.write_template_blob("u1", "фото", b"\xff\xd8\xff", ".jpg")
    storage.put_template("u1", "фото", {
        "kind": "photo", "file": blob, "ext": ".jpg", "mime": "image/jpeg"})
    client = FakeClient()

    async def _boom(chat, file=None, **kw):
        raise RuntimeError("FloodWaitError: ожидание 42с")

    client.send_file = _boom
    ev = FakeEvent(".шаб фото", client=client)
    _run(T.handle("u1", ev))
    assert "Не отправилось" in ev.out
    assert "FloodWaitError" in ev.out


def test_status_markers_are_correct(env):
    """[x] — это ошибка. На успехе должен стоять [+], иначе сообщение врёт."""
    ev = FakeEvent(".+шаб привет", reply=_reply("текст"))
    _run(T.handle("u1", ev))
    assert "[+] Шаблон сохранён" in ev.out
    assert "[x]" not in ev.out

    ev2 = FakeEvent(".-шаб привет")
    _run(T.handle("u1", ev2))
    assert "[+] Удалён шаблон" in ev2.out
    assert "[x]" not in ev2.out

    ev3 = FakeEvent(".-шаб несуществующий")
    _run(T.handle("u1", ev3))
    assert "[x]" in ev3.out


def test_fmt_size_units():
    assert T._fmt_size(0) == "0 Б"
    assert T._fmt_size(512) == "512 Б"
    assert T._fmt_size(2048) == "2 КБ"
    assert T._fmt_size(5 * 1024 * 1024) == "5 МБ"
    assert T._fmt_size(1024 ** 3) == "1.0 ГБ"


def test_plural_russian_forms():
    """«2 шаблона», а не «2 шаблонов» — иначе текст выглядит сломанным."""
    one, few, many = "шаблон", "шаблона", "шаблонов"
    assert T._plural(1, one, few, many) == one
    assert T._plural(2, one, few, many) == few
    assert T._plural(4, one, few, many) == few
    assert T._plural(5, one, few, many) == many
    assert T._plural(11, one, few, many) == many
    assert T._plural(12, one, few, many) == many
    assert T._plural(21, one, few, many) == one
    assert T._plural(22, one, few, many) == few
    assert T._plural(101, one, few, many) == one


def test_count_limit_message_is_grammatically_correct(env, monkeypatch):
    import config
    monkeypatch.setattr(config, "TEMPLATE_MAX_COUNT", 2)
    for i in range(2):
        ev = FakeEvent(f".+шаб ш{i}", reply=_reply(f"текст{i}"))
        _run(T.handle("u1", ev))
    ev = FakeEvent(".+шаб ш3", reply=_reply("ещё"))
    _run(T.handle("u1", ev))
    assert "2 шаблона" in ev.out
    assert "2 шаблонов" not in ev.out


def test_limits_are_generous():
    """Лимиты не должны artificially резать обычные ролики."""
    import config
    assert config.TEMPLATE_MAX_BYTES >= 50 * 1024 * 1024
    assert config.TEMPLATE_MAX_COUNT >= 500
    assert config.TEMPLATE_MAX_TOTAL_BYTES >= 1024 ** 3
    assert config.TEMPLATE_LIST_PAGE_SIZE >= 10


def test_bare_cmd_shows_help(env):
    ev = FakeEvent(".шаб")
    _run(T.handle("u1", ev))
    out = ev.out
    assert ".+шаб" in out and ".-шаб" in out and "список" in out
    assert not ev.deleted


def test_every_alias_reaches_help(env):
    for head in T.ALL_CMDS:
        ev = FakeEvent(head)
        _run(T.handle("u1", ev))
        # .+шаб / .-шаб без аргументов спрашивают имя, .шаб — справку
        assert ev.edited, head


def test_help_lists_all_kinds(env):
    ev = FakeEvent(".шаб")
    _run(T.handle("u1", ev))
    for kind in mk.KIND_ORDER:
        # в справке подписи типов выводятся в нижнем регистре
        assert mk.label(kind).lower() in ev.out, kind


# --------------------------------------------------------------------------
# .+шаб — сохранение
# --------------------------------------------------------------------------

def test_add_without_reply_asks_for_reply(env):
    ev = FakeEvent(".+шаб привет", reply=None)
    _run(T.handle("u1", ev))
    assert "ответить на сообщение" in ev.out
    assert storage.get_templates("u1") == {}


def test_add_without_name(env):
    ev = FakeEvent(".+шаб", reply=_reply("x"))
    _run(T.handle("u1", ev))
    assert "имя шаблона" in ev.out


def test_add_rejects_reserved_name(env):
    ev = FakeEvent(".+шаб список", reply=_reply("x"))
    _run(T.handle("u1", ev))
    assert "зарезервированное" in ev.out
    assert storage.get_templates("u1") == {}


def test_add_rejects_too_long_name(env, monkeypatch):
    import config
    monkeypatch.setattr(config, "TEMPLATE_NAME_MAX_LEN", 10)
    ev = FakeEvent(".+шаб " + "я" * 20, reply=_reply("x"))
    _run(T.handle("u1", ev))
    assert "длиннее" in ev.out


def test_add_text_template(env):
    ev = FakeEvent(".+шаб Привет", reply=_reply("всем привет"))
    _run(T.handle("u1", ev))
    rec = storage.get_template("u1", "привет")
    assert rec["kind"] == mk.KIND_TEXT
    assert rec["text"] == "всем привет"
    assert "сохранён" in ev.out
    # текстовый шаблон не тянет файл на диск
    assert not storage.template_dir("u1").exists()


def test_add_rejects_oversized_file(env, monkeypatch):
    import config
    monkeypatch.setattr(config, "TEMPLATE_MAX_BYTES", 10)
    ev = FakeEvent(".+шаб ролик",
                   reply=_reply("", _doc("video/mp4", 9999, [])))
    _run(T.handle("u1", ev))
    assert "Не сохраню" in ev.out
    assert "лимит на один шаблон" in ev.out
    assert "TEMPLATE_MAX_BYTES" in ev.out
    assert storage.get_templates("u1") == {}


def test_add_rejects_over_quota(env, monkeypatch):
    import config
    monkeypatch.setattr(config, "TEMPLATE_MAX_TOTAL_BYTES", 100)
    ev = FakeEvent(".+шаб ролик", reply=_reply("", _doc("video/mp4", 500, [])))
    _run(T.handle("u1", ev))
    assert "Не сохраню" in ev.out
    # показываем занято/лимит и конкретный шаг, а не просто «не хватит места»
    assert "занято" in ev.out and "TEMPLATE_MAX_TOTAL_BYTES" in ev.out
    assert ".-шаб" in ev.out
    assert storage.get_templates("u1") == {}


def test_add_rejects_when_count_limit_reached(env, monkeypatch):
    import config
    monkeypatch.setattr(config, "TEMPLATE_MAX_COUNT", 2)
    for i in range(2):
        ev = FakeEvent(f".+шаб ш{i}", reply=_reply(f"текст{i}"))
        _run(T.handle("u1", ev))
    ev = FakeEvent(".+шаб ш3", reply=_reply("ещё"))
    _run(T.handle("u1", ev))
    assert "Не сохраню" in ev.out
    assert "максимум" in ev.out
    assert "TEMPLATE_MAX_COUNT" in ev.out
    assert len(storage.get_templates("u1")) == 2


def test_success_card_shows_quota_usage(env):
    ev = FakeEvent(".+шаб ш", reply=_reply("текст"))
    _run(T.handle("u1", ev))
    assert "шаблонов: 1/" in ev.out
    assert "занято:" in ev.out


def test_add_overwrite_drops_old_file_on_ext_change(env):
    """Смена расширения при перезаписи не должна оставлять старый файл."""
    blob = storage.write_template_blob("u1", "поздравление", b"old", ".webm")
    storage.put_template("u1", "поздравление", {
        "kind": "video_note", "file": blob, "ext": ".webm",
        "mime": "video/webm", "size": 3})

    reply = _reply("", _doc("video/mp4", 3, [DocumentAttributeVideo(
        duration=2, w=200, h=200, round_message=True)]))

    async def _dl(file=None):
        return b"new"

    reply.download_media = _dl
    ev = FakeEvent(".+шаб поздравление", reply=reply)
    _run(T.handle("u1", ev))
    rec = storage.get_template("u1", "поздравление")
    assert rec["ext"] == ".mp4"
    files = list(storage.template_dir("u1").iterdir())
    assert len(files) == 1
    assert not (storage.template_dir("u1") / blob).exists()


def test_add_overwrite_frees_own_file_space(env, monkeypatch):
    """Перезапись не должна упираться в квоту, занятую самим же шаблоном."""
    import config
    monkeypatch.setattr(config, "TEMPLATE_MAX_TOTAL_BYTES", 100)
    for _ in range(2):
        reply = _reply("", _doc("video/mp4", 60, []))

        async def _dl(file=None):
            return b"x" * 60

        reply.download_media = _dl
        ev = FakeEvent(".+шаб ролик", reply=reply)
        _run(T.handle("u1", ev))
        assert "Не сохраню" not in ev.out, ev.out
    rec = storage.get_template("u1", "ролик")
    assert rec is not None and rec["size"] == 60
    assert storage.templates_total_bytes("u1") == 60


def _with_download(payload):
    reply = _reply("подпись", _doc("image/jpeg", 5, []))

    async def _dl(file=None):
        return payload

    reply.download_media = _dl
    return reply


def test_add_media_template_writes_file(env):
    ev = FakeEvent(".+шаб обложка", reply=_with_download(b"\xff\xd8\xffJPEG"))
    _run(T.handle("u1", ev))
    rec = storage.get_template("u1", "обложка")
    assert rec["kind"] == mk.KIND_PHOTO
    assert storage.read_template_blob("u1", rec) == b"\xff\xd8\xffJPEG"
    assert "подпись" in ev.out


def test_add_video_note_template(env):
    doc = _doc("video/mp4", 10, [DocumentAttributeVideo(
        duration=4, w=200, h=200, round_message=True)])

    async def _dl(file=None):
        return b"\x00\x00\x00\x18ftypmp42"

    reply = _reply("", doc)
    reply.download_media = _dl
    ev = FakeEvent(".+шаб поздравление", reply=reply)
    _run(T.handle("u1", ev))
    rec = storage.get_template("u1", "поздравление")
    assert rec["kind"] == mk.KIND_VIDEO_NOTE
    assert rec["ext"] == ".mp4"


def test_add_warns_when_download_fails(env):
    reply = _reply("", _doc("image/jpeg", 5, []))

    async def _boom(file=None):
        raise OSError("сеть")

    reply.download_media = _boom
    ev = FakeEvent(".+шаб обложка", reply=reply)
    _run(T.handle("u1", ev))
    assert "Не скачался" in ev.out
    assert storage.get_templates("u1") == {}


def test_add_empty_download_rejected(env):
    ev = FakeEvent(".+шаб обложка", reply=_with_download(b""))
    _run(T.handle("u1", ev))
    assert "пустой" in ev.out


def test_add_warns_voice_caption_will_be_lost(env):
    """Telegram не умеет caption к голосовому — предупреждаем при сохранении."""
    doc = _doc("audio/ogg", 5, [DocumentAttributeAudio(duration=5, voice=True)])

    async def _dl(file=None):
        return b"OggS"

    reply = _reply("а вот подпись", doc)
    reply.download_media = _dl
    ev = FakeEvent(".+шаб аудио", reply=reply)
    _run(T.handle("u1", ev))
    assert "не поедет" in ev.out or "не умеет подпись" in ev.out


# --------------------------------------------------------------------------
# .шаб <имя> — отправка
# --------------------------------------------------------------------------

def test_send_text_template(env):
    storage.put_template("u1", "привет", {"kind": "text", "text": "здарова"})
    ev = FakeEvent(".шаб привет")
    _run(T.handle("u1", ev))
    assert ev.deleted is True
    sent = ev.client.sent[0]
    assert sent["call"] == "message" and sent["text"] == "здарова"


def test_send_missing_template_lists_available(env):
    storage.put_template("u1", "привет", {"kind": "text", "text": "x"})
    ev = FakeEvent(".шаб отсутствует")
    _run(T.handle("u1", ev))
    assert "не найден" in ev.out and "привет" in ev.out
    assert not ev.deleted


def test_send_lost_blob_reports(env):
    storage.put_template("u1", "фото", {
        "kind": "photo", "file": "нет.jpg", "ext": ".jpg",
        "mime": "image/jpeg"})
    ev = FakeEvent(".шаб фото")
    _run(T.handle("u1", ev))
    assert "потерян" in ev.out
    assert not ev.deleted


def test_send_video_note_uses_video_note_flag(env):
    blob = storage.write_template_blob("u1", "кружок", b"\x00\x00\x00\x18ftyp", ".mp4")
    storage.put_template("u1", "кружок", {
        "kind": "video_note", "file": blob, "ext": ".mp4", "mime": "video/mp4"})
    ev = FakeEvent(".шаб кружок")
    _run(T.handle("u1", ev))
    sent = ev.client.sent[0]
    assert sent["call"] == "file"
    assert sent["video_note"] is True
    assert "force_document" not in sent


def test_send_voice_uses_voice_note_flag(env):
    blob = storage.write_template_blob("u1", "аудио", b"OggS-fake", ".ogg")
    storage.put_template("u1", "аудио", {
        "kind": "voice", "file": blob, "ext": ".ogg", "duration": 5})
    ev = FakeEvent(".шаб аудио")
    _run(T.handle("u1", ev))
    assert ev.client.sent[0]["voice_note"] is True


def test_send_photo_keeps_caption_as_plain_text(env):
    blob = storage.write_template_blob("u1", "обложка", b"\xff\xd8\xff", ".jpg")
    storage.put_template("u1", "обложка", {
        "kind": "photo", "file": blob, "ext": ".jpg",
        "mime": "image/jpeg", "text": "<b>жирный</b>"})
    ev = FakeEvent(".шаб обложка")
    _run(T.handle("u1", ev))
    sent = ev.client.sent[0]
    assert sent["force_document"] is False
    assert sent["caption"] == "<b>жирный</b>"
    # текст не должен интерпретироваться как HTML
    assert sent["parse_mode"] is None


def test_send_video_passes_streaming_attributes(env):
    blob = storage.write_template_blob("u1", "ролик", b"\x00\x00\x00\x18ftyp", ".mp4")
    storage.put_template("u1", "ролик", {
        "kind": "video", "file": blob, "ext": ".mp4", "mime": "video/mp4",
        "duration": 30, "w": 1280, "h": 720})
    ev = FakeEvent(".шаб ролик")
    _run(T.handle("u1", ev))
    sent = ev.client.sent[0]
    assert sent["supports_streaming"] is True
    attrs = {type(a).__name__: a for a in sent["attributes"]}
    assert attrs["DocumentAttributeVideo"].round_message is False
    assert attrs["DocumentAttributeVideo"].duration == 30


def test_send_animation_uses_animated_attribute(env):
    blob = storage.write_template_blob("u1", "гифка", b"GIF89a", ".gif")
    storage.put_template("u1", "гифка", {
        "kind": "animation", "file": blob, "ext": ".gif", "mime": "image/gif"})
    ev = FakeEvent(".шаб гифка")
    _run(T.handle("u1", ev))
    sent = ev.client.sent[0]
    assert any(type(a).__name__ == "DocumentAttributeAnimated"
               for a in sent["attributes"])


def test_send_document_uses_force_document(env):
    blob = storage.write_template_blob("u1", "файл", b"%PDF-1.7", ".pdf")
    storage.put_template("u1", "файл", {
        "kind": "document", "file": blob, "ext": ".pdf",
        "mime": "application/pdf"})
    ev = FakeEvent(".шаб файл")
    _run(T.handle("u1", ev))
    assert ev.client.sent[0]["force_document"] is True


def test_send_contact_uses_input_media(env):
    storage.put_template("u1", "визитка", {
        "kind": "contact", "phone": "+79991234567", "first_name": "Иван",
        "last_name": "Петров", "vcard": "BEGIN:VCARD", "user_id": 42})
    ev = FakeEvent(".шаб визитка")
    _run(T.handle("u1", ev))
    sent = ev.client.sent[0]
    assert sent["call"] == "message"
    assert type(sent["file"]).__name__ == "InputMediaContact"
    assert sent["file"].phone_number == "+79991234567"


def test_send_geo_uses_input_media(env):
    storage.put_template("u1", "точка", {
        "kind": "geo", "lat": 55.75, "long": 37.61})
    ev = FakeEvent(".шаб точка")
    _run(T.handle("u1", ev))
    sent = ev.client.sent[0]
    assert type(sent["file"]).__name__ == "InputMediaGeoPoint"
    assert sent["file"].geo_point.lat == 55.75
    assert sent["file"].geo_point.long == 37.61


def test_send_sticker_falls_back_to_document(env):
    blob = storage.write_template_blob("u1", "стикер", b"RIFF-webp", ".webp")
    storage.put_template("u1", "стикер", {
        "kind": "sticker", "file": blob, "ext": ".webp",
        "mime": "image/webp"})
    client = FakeClient()
    calls = {"n": 0}
    original = client.send_file

    async def flaky(chat, file=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("стикер не принят")
        return await original(chat, file=file, **kw)

    client.send_file = flaky
    ev = FakeEvent(".шаб стикер", client=client)
    _run(T.handle("u1", ev))
    assert calls["n"] == 2
    assert ev.client.sent[0]["force_document"] is True
    assert ev.deleted is True


def test_send_reports_client_failure(env):
    blob = storage.write_template_blob("u1", "фото", b"\xff\xd8\xff", ".jpg")
    storage.put_template("u1", "фото", {
        "kind": "photo", "file": blob, "ext": ".jpg", "mime": "image/jpeg"})
    client = FakeClient()

    async def _boom(chat, file=None, **kw):
        raise RuntimeError("FloodWait")

    client.send_file = _boom
    ev = FakeEvent(".шаб фото", client=client)
    _run(T.handle("u1", ev))
    assert "Не отправилось" in ev.out
    assert not ev.deleted


def test_send_uses_custom_emoji_only_with_premium(env, monkeypatch):
    storage.put_template("u1", "ролик", {"kind": "video", "name": "ролик"})

    async def _yes(client=None):
        return True

    monkeypatch.setattr(mk, "_VALID_IDS",
                        {mk.KINDS[mk.KIND_VIDEO].premium_id})
    monkeypatch.setattr(T, "_premium", _yes)
    ev = FakeEvent(".шаб список")
    _run(T.handle("u1", ev))
    assert "tg-emoji" in ev.out

    ev2 = FakeEvent(".шаб список")
    monkeypatch.setattr(T, "_premium", lambda client=None: _no_premium())
    _run(T.handle("u1", ev2))
    assert "tg-emoji" not in ev2.out


async def _no_premium():
    return False


def test_premium_ids_not_emitted_when_unvalidated(env, monkeypatch):
    """Даже с Premium не выпускаем в разметку невалидированные id."""
    storage.put_template("u1", "ролик", {"kind": "video", "name": "ролик"})

    async def _yes(client=None):
        return True

    monkeypatch.setattr(mk, "_VALID_IDS", set())
    monkeypatch.setattr(T, "_premium", _yes)
    ev = FakeEvent(".шаб список")
    _run(T.handle("u1", ev))
    assert "tg-emoji" not in ev.out


# --------------------------------------------------------------------------
# .-шаб — удаление
# --------------------------------------------------------------------------

def test_del_removes_template_and_file(env):
    blob = storage.write_template_blob("u1", "фото", b"data", ".jpg")
    storage.put_template("u1", "фото", {"kind": "photo", "file": blob})
    ev = FakeEvent(".-шаб фото")
    _run(T.handle("u1", ev))
    assert storage.get_template("u1", "фото") is None
    assert "Удалён" in ev.out


def test_del_missing_reports(env):
    ev = FakeEvent(".-шаб нету")
    _run(T.handle("u1", ev))
    assert "не найден" in ev.out


def test_del_without_name_lists(env):
    storage.put_template("u1", "фото", {"kind": "photo", "text": ""})
    ev = FakeEvent(".-шаб")
    _run(T.handle("u1", ev))
    assert "имя шаблона" in ev.out and "фото" in ev.out


# --------------------------------------------------------------------------
# .шаб список — пагинация
# --------------------------------------------------------------------------

def test_list_empty(env):
    ev = FakeEvent(".шаб список")
    _run(T.handle("u1", ev))
    assert "Шаблонов пока нет" in ev.out


def test_list_paginates(env, monkeypatch):
    import config
    monkeypatch.setattr(config, "TEMPLATE_LIST_PAGE_SIZE", 3)
    for i in range(7):
        storage.put_template("u1", f"ш{i}", {"kind": "text", "text": "x"})
    pages = []
    for n in (1, 2, 3):
        ev = FakeEvent(f".шаб список {n}" if n > 1 else ".шаб список")
        _run(T.handle("u1", ev))
        pages.append(ev.out)
        assert f"стр. {n}/3" in ev.out
    assert pages[0].count("<code>ш") == 3
    assert pages[2].count("<code>ш") == 1
    # навигация: на 1й только вперёд, на последней только назад
    assert "←" not in pages[0] and "→" in pages[0]
    assert "→" not in pages[2] and "←" in pages[2]


def test_list_out_of_range(env, monkeypatch):
    import config
    monkeypatch.setattr(config, "TEMPLATE_LIST_PAGE_SIZE", 2)
    for i in range(3):
        storage.put_template("u1", f"ш{i}", {"kind": "text", "text": "x"})
    ev = FakeEvent(".шаб список 99")
    _run(T.handle("u1", ev))
    assert "Страницы 99 нет" in ev.out


def test_list_rejects_zero_page(env):
    storage.put_template("u1", "ш", {"kind": "text", "text": "x"})
    ev = FakeEvent(".шаб список 0")
    _run(T.handle("u1", ev))
    assert "нет" in ev.out


def test_list_rejects_non_numeric_page(env):
    storage.put_template("u1", "ш", {"kind": "text", "text": "x"})
    ev = FakeEvent(".шаб список абв")
    _run(T.handle("u1", ev))
    assert "не номер страницы" in ev.out


def test_list_word_aliases(env):
    storage.put_template("u1", "ш", {"kind": "text", "text": "x"})
    for word in ("список", "list", "ls", "СПИСОК", "List"):
        ev = FakeEvent(f".шаб {word}")
        _run(T.handle("u1", ev))
        assert "<code>ш</code>" in ev.out, word


def test_template_name_escaped_in_list(env):
    storage.put_template("u1", "<script>", {"kind": "text", "text": "x"})
    ev = FakeEvent(".шаб список")
    _run(T.handle("u1", ev))
    assert "<script>" not in ev.out
    assert "&lt;script&gt;" in ev.out


def test_list_sorted_by_kind_then_name(env):
    storage.put_template("u1", "zzz", {"kind": "video", "text": ""})
    storage.put_template("u1", "beta", {"kind": "photo", "text": ""})
    storage.put_template("u1", "alpha", {"kind": "photo", "text": ""})
    storage.put_template("u1", "aaa", {"kind": "text", "text": ""})
    ev = FakeEvent(".шаб список")
    _run(T.handle("u1", ev))
    body = ev.out
    assert (body.index("aaa") < body.index("alpha") < body.index("beta")
            < body.index("zzz"))
