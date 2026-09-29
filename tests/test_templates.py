"""Тесты `.шаб` — шаблоны сообщений.

Покрывает три слоя:
  1. utils/media_kind.py — классификация сообщений и иконки типов;
  2. utils/storage.py   — персистентность шаблонов (CRUD + файлы на диске);
  3. handlers/commands/template.py — команды .шаб / .+шаб / .-шаб / список.
"""

import io
from types import SimpleNamespace

import pytest
from telethon.tl.types import (
    Document, DocumentAttributeAnimated, DocumentAttributeAudio,
    DocumentAttributeFilename, DocumentAttributeSticker, DocumentAttributeVideo,
    GeoPoint, InputStickerSetEmpty, MessageMediaContact, Photo, PhotoSize,
)

from utils import media_kind as mk
from utils import storage


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _ns(**kw):
    return SimpleNamespace(**kw)


def _doc(mime="", size=0, attributes=None):
    return Document(id=1, access_hash=2, file_reference=b"", date=None,
                    mime_type=mime, size=size, dc_id=2,
                    attributes=list(attributes or []))


def _video_attr(duration=0, w=0, h=0, round_message=False):
    return DocumentAttributeVideo(duration=duration, w=w, h=h,
                                  round_message=round_message)


def _audio_attr(duration=0, voice=False, title=None, performer=None):
    return DocumentAttributeAudio(duration=duration, voice=voice, title=title,
                                  performer=performer)


def _sticker_attr():
    return DocumentAttributeSticker(alt="", stickerset=InputStickerSetEmpty())


def _photo(sizes=((800, 600, 4000),), ):
    return Photo(
        id=1, access_hash=2, file_reference=b"", date=None, dc_id=2,
        sizes=[PhotoSize(type="x", w=w, h=h, size=sz) for w, h, sz in sizes],
    )


def _text_msg(text="привет"):
    return _ns(message=text, raw_text=text, caption=None, document=None)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Изолированное хранилище шаблонов в tmp_path."""
    monkeypatch.setattr(storage, "TEMPLATES_FILE", tmp_path / "templates.json")
    monkeypatch.setattr(storage, "TEMPLATES_DIR", tmp_path / "templates")
    monkeypatch.setattr(storage, "templates", {})
    return storage


# --------------------------------------------------------------------------
# media_kind.classify
# --------------------------------------------------------------------------

def test_classify_plain_text():
    kind, meta = mk.classify(_text_msg())
    assert kind == mk.KIND_TEXT
    assert meta["text"] == "привет"


def test_classify_photo_reads_dimensions_from_sizes():
    """У Photo нет .dims — размеры лежат в .sizes, берём наибольший."""
    msg = _ns(message="", photo=_photo([(100, 80, 10), (800, 600, 4000)]),
              document=None)
    kind, meta = mk.classify(msg)
    assert kind == mk.KIND_PHOTO
    assert (meta["w"], meta["h"]) == (800, 600)
    assert meta["size"] == 4000
    assert mk.KINDS[kind].has_file is True


def test_classify_video_with_duration_and_dims():
    msg = _ns(message="крутое видео", document=_doc(
        "video/mp4", 999, [_video_attr(duration=12, w=1280, h=720)]))
    kind, meta = mk.classify(msg)
    assert kind == mk.KIND_VIDEO
    assert meta["duration"] == 12
    assert (meta["w"], meta["h"]) == (1280, 720)
    assert meta["text"] == "крутое видео"


def test_classify_video_note_is_distinct_from_video():
    """Кружок обязан отличаться от видео: при отправке идёт video_note=True."""
    msg = _ns(message="", document=_doc(
        "video/mp4", 10, [_video_attr(duration=3, w=200, h=200,
                                     round_message=True)]))
    assert mk.classify(msg)[0] == mk.KIND_VIDEO_NOTE


def test_classify_voice():
    msg = _ns(message="", document=_doc(
        "audio/ogg", 500, [_audio_attr(duration=8, voice=True)]))
    kind, meta = mk.classify(msg)
    assert kind == mk.KIND_VOICE
    assert meta["duration"] == 8


def test_classify_audio_document_with_title():
    msg = _ns(message="", document=_doc(
        "audio/mpeg", 3000, [_audio_attr(duration=180, title="Song",
                                        performer="Artist")]))
    kind, meta = mk.classify(msg)
    assert kind == mk.KIND_AUDIO
    assert meta["title"] == "Song"
    assert meta["performer"] == "Artist"
    assert meta["ext"] == ".mp3"


def test_classify_animation_via_animated_attribute():
    """Telethon 1.44 не имеет message.animation — GIF ловим по атрибуту."""
    msg = _ns(message="", document=_doc("video/mp4", 99,
                                        [DocumentAttributeAnimated()]))
    assert mk.classify(msg)[0] == mk.KIND_ANIMATION


def test_classify_sticker_via_attribute():
    msg = _ns(message="", document=_doc("image/webp", 10, [_sticker_attr()]))
    assert mk.classify(msg)[0] == mk.KIND_STICKER


def test_classify_animated_sticker_mime_without_sticker_attr():
    """Старый формат: TGS-документ без атрибута sticker — тоже стикер."""
    msg = _ns(message="", document=_doc("application/x-tgsticker", 5, []))
    assert mk.classify(msg)[0] == mk.KIND_STICKER


def test_classify_pdf_document():
    msg = _ns(message="", document=_doc("application/pdf", 5, []))
    assert mk.classify(msg)[0] == mk.KIND_DOCUMENT


def test_classify_image_document_counts_as_photo():
    msg = _ns(message="", document=_doc("image/png", 5, []))
    assert mk.classify(msg)[0] == mk.KIND_PHOTO


def test_classify_video_document_by_mime():
    msg = _ns(message="", document=_doc("video/mp4", 5, []))
    assert mk.classify(msg)[0] == mk.KIND_VIDEO


def test_classify_contact():
    msg = _ns(message="", document=None, contact=MessageMediaContact(
        phone_number="+79991234567", first_name="Иван", last_name="Петров",
        vcard="BEGIN:VCARD", user_id=42))
    kind, meta = mk.classify(msg)
    assert kind == mk.KIND_CONTACT
    assert meta["phone"] == "+79991234567"
    assert meta["first_name"] == "Иван"
    assert meta["user_id"] == 42
    assert mk.KINDS[kind].has_file is False


def test_classify_geo_reads_bare_geopoint():
    """Message.geo — сам GeoPoint, а не обёртка с .geo внутри."""
    msg = _ns(message="", document=None, contact=None,
              geo=GeoPoint(long=37.61, lat=55.75, access_hash=0))
    kind, meta = mk.classify(msg)
    assert kind == mk.KIND_GEO
    assert meta["lat"] == 55.75
    assert meta["long"] == 37.61
    assert mk.KINDS[kind].has_file is False


def test_classify_caption_used_when_no_message():
    msg = _ns(message=None, raw_text=None, caption="подпись к фото",
              photo=_photo([(10, 10, 1)]), document=None)
    kind, meta = mk.classify(msg)
    assert kind == mk.KIND_PHOTO
    assert meta["text"] == "подпись к фото"


def test_classify_ext_prefers_document_filename():
    msg = _ns(message="", document=_doc(
        "application/octet-stream", 5, [DocumentAttributeFilename(
            file_name="отчёт.final.docx")]))
    _, meta = mk.classify(msg)
    assert meta["ext"] == ".docx"


def test_classify_ext_rejects_absurd_extension():
    msg = _ns(message="", document=_doc(
        "application/octet-stream", 5, [DocumentAttributeFilename(
            file_name="файл.оченьдлинноерасширение")]))
    _, meta = mk.classify(msg)
    assert meta["ext"] == ".bin"


def test_classify_none_message():
    kind, meta = mk.classify(None)
    assert kind == mk.KIND_TEXT
    assert meta["text"] == ""


# --------------------------------------------------------------------------
# media_kind: иконки
# --------------------------------------------------------------------------

def test_icon_is_unicode_without_premium():
    assert mk.icon(mk.KIND_VIDEO) == "\U0001F3A5"


def test_icon_is_unicode_when_premium_but_ids_unvalidated(monkeypatch):
    """Невалидированные id в разметку не пускаем: Telegram всё равно
    нарисует fallback, а в HTML останется мёртвый emoji-id."""
    monkeypatch.setattr(mk, "_VALID_IDS", set())
    out = mk.icon(mk.KIND_VIDEO, premium=True)
    assert "tg-emoji" not in out
    assert out == "\U0001F3A5"


def test_icon_uses_custom_emoji_when_validated(monkeypatch):
    spec = mk.KINDS[mk.KIND_VIDEO]
    monkeypatch.setattr(mk, "_VALID_IDS", {spec.premium_id})
    out = mk.icon(mk.KIND_VIDEO, premium=True)
    assert out == f'<tg-emoji emoji-id="{spec.premium_id}">\U0001F3A5</tg-emoji>'


def test_icon_falls_back_for_unknown_kind():
    assert mk.icon("nope") == mk.icon(mk.KIND_TEXT)


def test_every_kind_has_unique_premium_id_and_label():
    ids = [s.premium_id for s in mk.KINDS.values() if s.premium_id]
    assert len(ids) == len(set(ids)), "premium_id не должны повторяться"
    for key, spec in mk.KINDS.items():
        assert spec.label, key
        assert spec.fallback, key


def test_kinds_with_file_matches_spec_flag():
    for key, spec in mk.KINDS.items():
        assert spec.has_file == (key in mk.KINDS_WITH_FILE), key


def test_kind_order_covers_all_kinds():
    assert set(mk.KIND_ORDER) == set(mk.KINDS)


# --------------------------------------------------------------------------
# media_kind: describe / sort
# --------------------------------------------------------------------------

def test_describe_escapes_template_name():
    """Имя шаблона приходит от юзера — нельзя дать ему внедрить HTML."""
    line = mk.describe(mk.KIND_TEXT, {"name": "<b>жирный</b>"})
    assert "&lt;b&gt;" in line
    assert "<b>жирный</b>" not in line


def test_describe_shows_duration_for_voice():
    line = mk.describe(mk.KIND_VOICE, {"name": "аудио", "duration": 75})
    assert "1:15" in line


def test_describe_short_duration_has_no_minutes():
    line = mk.describe(mk.KIND_VIDEO,
                       {"name": "ролик", "duration": 10, "w": 1280, "h": 720})
    assert "10с" in line and "1280×720" in line


def test_describe_marks_caption_media():
    line = mk.describe(mk.KIND_PHOTO, {"name": "обложка", "text": "привет"})
    assert "с подписью" in line
    assert "с подписью" not in mk.describe(mk.KIND_PHOTO,
                                           {"name": "обложка", "text": ""})


def test_sort_records_groups_by_kind_then_name():
    bucket = {
        "b": {"kind": mk.KIND_VIDEO, "name": "zzz"},
        "a": {"kind": mk.KIND_PHOTO, "name": "beta"},
        "c": {"kind": mk.KIND_PHOTO, "name": "alpha"},
        "d": {"kind": mk.KIND_TEXT, "name": "aaa"},
    }
    got = [r["name"] for r in mk.sort_records(bucket)]
    assert got == ["aaa", "alpha", "beta", "zzz"]


# --------------------------------------------------------------------------
# storage: шаблоны
# --------------------------------------------------------------------------

def test_normalize_template_name():
    assert storage.normalize_template_name("  ПРИВЕТ   Мир ") == "привет мир"
    assert storage.normalize_template_name("") == ""


def test_template_blob_name_is_path_safe():
    """Имя может содержать `/` и `..` — в имя файла оно попасть не должно."""
    blob = storage.template_blob_name("../../etc/passwd", "jpg")
    assert "/" not in blob
    assert ".." not in blob
    assert blob.endswith(".jpg")


def test_put_get_delete_roundtrip(store):
    store.put_template("u1", "Привет", {"kind": "text", "text": "hi"})
    got = store.get_template("u1", "ПРИВЕТ")
    assert got["text"] == "hi"
    # display-имя сохраняем как ввёл юзер, ключ — нормализованный
    assert got["name"] == "Привет"
    assert "привет" in store.get_templates("u1")
    assert store.delete_template("u1", "привет") is not None
    assert store.get_template("u1", "привет") is None


def test_delete_removes_blob_file(store):
    blob = store.write_template_blob("u1", "фото", b"bytes", ".jpg")
    store.put_template("u1", "фото", {"kind": "photo", "file": blob, "size": 5})
    path = store.template_dir("u1") / blob
    assert path.exists()
    store.delete_template("u1", "фото")
    assert not path.exists()


def test_overwrite_leaves_no_orphan_file(store):
    """Смена расширения при перезаписи не должна оставлять старый blob."""
    old_blob = store.write_template_blob("u1", "поздравление", b"old", ".webm")
    store.put_template("u1", "поздравление",
                       {"kind": "video_note", "file": old_blob, "size": 3})
    new_blob = store.write_template_blob("u1", "поздравление", b"new", ".mp4")
    store.put_template("u1", "поздравление",
                       {"kind": "video_note", "file": new_blob, "size": 3})
    store.drop_template_blob("u1", {"file": old_blob})
    files = list(store.template_dir("u1").iterdir())
    assert len(files) == 1 and files[0].name == new_blob


def test_read_template_blob_missing_returns_none(store):
    rec = {"kind": "photo", "file": "нет-такого.jpg"}
    assert store.read_template_blob("u1", rec) is None
    assert store.template_blob_path("u1", {"kind": "text"}) is None


def test_templates_total_bytes(store):
    store.put_template("u1", "a", {"kind": "text", "size": 100})
    store.put_template("u1", "b", {"kind": "text", "size": 250})
    assert store.templates_total_bytes("u1") == 350
    assert store.templates_total_bytes("u2") == 0


def test_load_templates_skips_broken_records(store, tmp_path):
    import json
    (tmp_path / "templates.json").write_text(json.dumps({
        "u1": {
            "ok": {"kind": "text", "text": "x"},
            "no_kind": {"text": "битая"},
            "not_dict": "строка",
        },
        "u2": "не словарь",
    }), encoding="utf-8")
    store.load_templates()
    assert set(store.get_templates("u1")) == {"ok"}
    assert store.get_templates("u2") == {}


def test_delete_unknown_returns_none(store):
    assert store.delete_template("u1", "нет") is None


def test_save_reload_persists(store, tmp_path):
    store.put_template("u1", "тест", {"kind": "text", "text": "данные"})
    assert (tmp_path / "templates.json").exists()
    store.templates.clear()
    store.load_templates()
    assert store.get_template("u1", "тест")["text"] == "данные"
