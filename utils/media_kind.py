"""Классификация сообщений для `.шаб` + премиум-эмодзи по типам шаблонов.

Единая точка правды о том, «что за сообщение»: `.шаб` сохраняет реплай и
отправляет его заново позже, поэтому тип нужно определить точно — от него
зависит и расширение файла, и то, как мы переотправим медиа (кружок должен
вернуться кружком, а не mp4-файлом).

В отличие от ``handlers/commands/quote.py`` (``_reply_media_kind``), который
возвращает грубую категорию для рендера цитаты, здесь нужен полный набор
типов + метаданные для переотправки (mime, размеры, длительность, поля
контакта/координат).

Типы (``KIND_*``):
    text, photo, video, animation, video_note, voice, audio,
    sticker, document, contact, geo
"""

from __future__ import annotations

import logging

from utils.escape import esc as _esc
import os
from dataclasses import dataclass

from telethon.tl.types import (
    DocumentAttributeAnimated, DocumentAttributeAudio,
    DocumentAttributeFilename, DocumentAttributeSticker,
    DocumentAttributeVideo, Photo, PhotoCachedSize, PhotoSize,
)

logger = logging.getLogger(__name__)

KIND_TEXT = "text"
KIND_PHOTO = "photo"
KIND_VIDEO = "video"
KIND_ANIMATION = "animation"
KIND_VIDEO_NOTE = "video_note"
KIND_VOICE = "voice"
KIND_AUDIO = "audio"
KIND_STICKER = "sticker"
KIND_DOCUMENT = "document"
KIND_CONTACT = "contact"
KIND_GEO = "geo"

# Типы, у которых тело шаблона — файл на диске (остальные хранятся как записи).
KINDS_WITH_FILE = frozenset({
    KIND_PHOTO, KIND_VIDEO, KIND_ANIMATION, KIND_VIDEO_NOTE,
    KIND_VOICE, KIND_AUDIO, KIND_STICKER, KIND_DOCUMENT,
})

_ANIMATED_STICKER_MIMES = frozenset({
    "application/x-tgsticker", "application/x-tgs-sticker",
})

_EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "application/x-tgsticker": ".tgs",
}


@dataclass(frozen=True)
class KindSpec:
    """Описание типа шаблона: подпись в списке + эмодзи-индикатор."""

    key: str
    label: str
    fallback: str
    #: Telegram custom emoji document id (анимированная версия иконки).
    #: Показывается только юзеру с Premium и только после успешной
    #: валидации id (см. :func:`resolve_custom_emoji`).
    premium_id: int | None = None
    #: Нужно ли качать файл (True) или хватит записи в templates.json (False).
    has_file: bool = True
    comment: str = ""


# Иконки типов. custom-emoji id выбраны из реально существующих документов
# Telegram (проверено GetCustomEmojiDocumentsRequest): id валиден — значит
# премиум-юзер увидит анимированную иконку, остальные — unicode-фолбэк.
#
# ВАЖНО: id не «навсегда». Если набор удалят, resolve_custom_emoji() это
# обнаружит (документ не вернётся) и вернёт fallback для этого типа.
KINDS: dict[str, KindSpec] = {
    KIND_TEXT: KindSpec(KIND_TEXT, "Текст", "\U0001F4AC", 5269564073464334732,
                        False, "обычное сообщение / подпись к медиа"),
    KIND_PHOTO: KindSpec(KIND_PHOTO, "Фото", "\U0001F4F7", 5987917196469213507,
                         True, "reply.photo"),
    KIND_VIDEO: KindSpec(KIND_VIDEO, "Видео", "\U0001F3A5", 5280662183057825163,
                         True, "reply.video / mp4-документ"),
    KIND_ANIMATION: KindSpec(KIND_ANIMATION, "GIF", "✨", 5280723506600911579,
                             True, "reply.animation"),
    KIND_VIDEO_NOTE: KindSpec(KIND_VIDEO_NOTE, "Кружок", "⭕", 6032984490169605484,
                              True, "reply.video_note (round video)"),
    KIND_VOICE: KindSpec(KIND_VOICE, "Голосовое", "\U0001F3A4", 5258500422393415126,
                         True, "reply.voice"),
    KIND_AUDIO: KindSpec(KIND_AUDIO, "Аудио", "\U0001F3A7", 5287380220578372452,
                         True, "reply.audio / mp3-документ"),
    KIND_STICKER: KindSpec(KIND_STICKER, "Стикер", "\U0001F308", 60125290227634244,
                           True, "reply.sticker (статичный и анимированный)"),
    KIND_DOCUMENT: KindSpec(KIND_DOCUMENT, "Документ", "\U0001F4C1", 5341492148468465410,
                            True, "всё остальное с файлом"),
    KIND_CONTACT: KindSpec(KIND_CONTACT, "Контакт", "\U0001F464", 5879770735999717115,
                           False, "reply.contact — vcard в записи шаблона"),
    KIND_GEO: KindSpec(KIND_GEO, "Координаты", "\U0001F4CD", 5422548510740349734,
                       False, "reply.geo — lat/lon в записи шаблона"),
}

#: Порядок в `.шаб список` — сначала то, что чаще пересылают.
KIND_ORDER = (
    KIND_TEXT, KIND_PHOTO, KIND_VIDEO, KIND_ANIMATION, KIND_VIDEO_NOTE,
    KIND_VOICE, KIND_AUDIO, KIND_STICKER, KIND_DOCUMENT, KIND_CONTACT, KIND_GEO,
)

#: Насколько «заметный» тип при сортировке списка (меньше — выше).
KIND_RANK = {k: i for i, k in enumerate(KIND_ORDER)}


def icon(kind: str, *, premium: bool = False) -> str:
    """Иконка типа шаблона.

    ``premium=True`` отдаёт ``<tg-emoji emoji-id="...">``, но только если id
    прошёл валидацию (:func:`resolve_custom_emoji` кладёт результат в
    ``_VALID_IDS``). Без валидации — всегда unicode, потому что
    Telegram просто нарисует fallback, а битый id — это мусор в разметке.
    """
    spec = KINDS.get(kind) or KINDS[KIND_TEXT]
    if premium and spec.premium_id and spec.premium_id in _VALID_IDS:
        return f'<tg-emoji emoji-id="{spec.premium_id}">{spec.fallback}</tg-emoji>'
    return spec.fallback


def label(kind: str) -> str:
    return (KINDS.get(kind) or KINDS[KIND_TEXT]).label


# ----------------- валидация custom-emoji id -----------------
# Один GetCustomEmojiDocumentsRequest на все id сразу (их меньше 20),
# результат кэшируется на час. Ошибка сети → просто не включаем premium.
_VALID_IDS: set[int] = set()
_valid_checked_at: float = 0.0
_VALID_LOCK: "asyncio.Lock | None" = None

_PREMIUM_IDS = tuple(
    spec.premium_id for spec in KINDS.values() if spec.premium_id
)
_VALID_TTL = 3600.0


async def resolve_custom_emoji(client, *, force: bool = False) -> set[int]:
    """Проверяет, какие custom-emoji id ещё живы. Возвращает множество.

    Кэш на час: id меняются крайне редко, а запрос стоит денег/rate-limit.
    Любая ошибка → пустое множество (все иконки станут unicode).
    """
    global _VALID_IDS, _VALID_checked_at, _VALID_LOCK
    import time
    now = time.time()
    if not force and _VALID_IDS and now - _VALID_checked_at < _VALID_TTL:
        return set(_VALID_IDS)
    if _VALID_LOCK is None:
        import asyncio
        _VALID_LOCK = asyncio.Lock()
    async with _VALID_LOCK:
        now = time.time()
        if not force and _VALID_IDS and now - _VALID_checked_at < _VALID_TTL:
            return set(_VALID_IDS)
        found: set[int] = set()
        if client is not None and _PREMIUM_IDS:
            try:
                import asyncio as _a
                from telethon.tl.functions.messages import (
                    GetCustomEmojiDocumentsRequest,
                )
                docs = await _a.wait_for(
                    client(GetCustomEmojiDocumentsRequest(
                        document_id=list(_PREMIUM_IDS))),
                    timeout=15,
                )
                found = {d.id for d in (docs or [])}
            except Exception as e:
                logger.debug("media_kind: custom emoji probe failed: %s", e)
        _VALID_IDS = found
        _VALID_checked_at = time.time()
        return set(found)


def _doc_attrs(msg) -> list:
    return list(getattr(getattr(msg, "document", None), "attributes", None) or [])


def _has_attr(msg, cls) -> bool:
    """Есть ли среди атрибутов документа экземпляр cls."""
    return any(isinstance(a, cls) for a in _doc_attrs(msg))


def _audio_attr(msg):
    for a in _doc_attrs(msg):
        if isinstance(a, DocumentAttributeAudio):
            return a
    return None


def _mime(msg) -> str:
    return (getattr(getattr(msg, "document", None), "mime_type", "") or "").lower()


def _ext_for(msg, mime: str) -> str:
    """Расширение для файла шаблона: из имени документа, иначе из mime."""
    for a in _doc_attrs(msg):
        if isinstance(a, DocumentAttributeFilename):
            ext = os.path.splitext(a.file_name or "")[1]
            if ext and len(ext) <= 6:
                return ext.lower()
    return _EXT_BY_MIME.get(mime, ".bin")


def _size_of(msg) -> int:
    doc = getattr(msg, "document", None)
    size = getattr(doc, "size", None)
    if isinstance(size, int) and size > 0:
        return size
    # У Photo нет .size — размеры лежат в .sizes; берём наибольший.
    photo = getattr(msg, "photo", None)
    best = 0
    for s in (getattr(photo, "sizes", None) or []):
        if isinstance(s, (PhotoSize, PhotoCachedSize)) and s.size > best:
            best = int(s.size)
    return best


def _duration_of(msg) -> int:
    audio = _audio_attr(msg)
    d = getattr(audio, "duration", None)
    if not d:
        for a in _doc_attrs(msg):
            if isinstance(a, DocumentAttributeVideo) and a.duration:
                d = a.duration
                break
    return int(d) if isinstance(d, (int, float)) and d > 0 else 0


def _dims_of(msg) -> tuple[int, int]:
    for a in _doc_attrs(msg):
        if isinstance(a, DocumentAttributeVideo) and a.w and a.h:
            return int(a.w or 0), int(a.h or 0)
    photo = getattr(msg, "photo", None)
    if isinstance(photo, Photo):
        best, dims = 0, (0, 0)
        for s in (photo.sizes or []):
            if isinstance(s, (PhotoSize, PhotoCachedSize)) and s.w * s.h > best:
                best, dims = s.w * s.h, (int(s.w or 0), int(s.h or 0))
        if dims != (0, 0):
            return dims
    return 0, 0


def _contact_fields(msg) -> dict:
    # Message.contact уже развёрнут до MessageMediaContact (Telethon ≥1.42).
    c = getattr(msg, "contact", None)
    if c is None:
        return {}
    return {
        "phone": getattr(c, "phone_number", "") or "",
        "first_name": getattr(c, "first_name", "") or "",
        "last_name": getattr(c, "last_name", "") or "",
        "vcard": getattr(c, "vcard", "") or "",
        "user_id": int(getattr(c, "user_id", 0) or 0),
    }


def _geo_fields(msg) -> dict:
    # Message.geo — сам GeoPoint (Telethon разворачивает MessageMediaGeo.geo).
    g = getattr(msg, "geo", None)
    if g is None:
        return {}
    lat, long_ = getattr(g, "lat", None), getattr(g, "long", None)
    if lat is None or long_ is None:
        return {}
    return {"lat": float(lat), "long": float(long_)}


def _kind_from_attrs(msg) -> str | None:
    """Тип по атрибутам документа — единственный источник правды.

    Telethon-овские свойства (.voice/.sticker/.video_note/.gif) — это
    обёртки над теми же атрибутами, но они есть не во всех версиях
    (например, поля ``message.animation`` в 1.44 уже нет). Поэтому читаем
    атрибуты напрямую: работает одинаково на любой версии и тестируется
    без сборки полного Message.

    Порядок важен:
      стикер → кружок (round_message) → анимация → видео → голосовое → аудио.
    Анимация проверяется до видео, потому что «GIF» у Telegram — это mp4
    с DocumentAttributeAnimated И DocumentAttributeVideo одновременно.
    """
    attrs = _doc_attrs(msg)
    if any(isinstance(a, DocumentAttributeSticker) for a in attrs):
        return KIND_STICKER
    for a in attrs:
        if isinstance(a, DocumentAttributeVideo) and a.round_message:
            return KIND_VIDEO_NOTE
    if any(isinstance(a, DocumentAttributeAnimated) for a in attrs):
        return KIND_ANIMATION
    if any(isinstance(a, DocumentAttributeVideo) for a in attrs):
        return KIND_VIDEO
    audio = _audio_attr(msg)
    if audio is not None:
        return KIND_VOICE if getattr(audio, "voice", False) else KIND_AUDIO
    return None


def classify(msg) -> tuple[str, dict]:
    """Определяет тип шаблона + метаданные для переотправки. Pure (сеть не нужна).

    Returns:
        (kind, meta) где meta — словарь, готовый к ``storage.put_template``.
        Для типов с файлом там ``mime``/``ext``/``size``; для контакта и
        координат — сами поля; для текста — ``text`` (может быть пустым).
    """
    if msg is None:
        return KIND_TEXT, {"text": ""}

    mime = _mime(msg)
    contact = _contact_fields(msg)
    if contact:
        return KIND_CONTACT, contact

    geo = _geo_fields(msg)
    if geo:
        return KIND_GEO, geo

    kind = _kind_from_attrs(msg)
    if kind is None:
        if isinstance(getattr(msg, "photo", None), Photo):
            kind = KIND_PHOTO
        elif mime in _ANIMATED_STICKER_MIMES:
            kind = KIND_STICKER
        elif mime.startswith("video/"):
            kind = KIND_VIDEO
        elif mime.startswith("audio/"):
            kind = KIND_AUDIO
        elif mime.startswith("image/"):
            kind = KIND_PHOTO
        elif getattr(msg, "document", None) is not None:
            kind = KIND_DOCUMENT
        else:
            return KIND_TEXT, {"text": _message_text(msg)}

    meta: dict = {
        "mime": mime,
        "size": _size_of(msg),
        "duration": _duration_of(msg),
        "ext": _ext_for(msg, mime),
        "text": _message_text(msg),
    }
    w, h = _dims_of(msg)
    if w:
        meta["w"] = w
    if h:
        meta["h"] = h
    if kind == KIND_AUDIO:
        audio = _audio_attr(msg)
        if getattr(audio, "title", None):
            meta["title"] = audio.title
        if getattr(audio, "performer", None):
            meta["performer"] = audio.performer
    return kind, meta


def _message_text(msg) -> str:
    """Текст сообщения или caption (то, что станет подписью к медиа)."""
    t = getattr(msg, "message", None) or getattr(msg, "raw_text", None)
    if t:
        return str(t)
    return str(getattr(msg, "caption", "") or "")


def describe(kind: str, record: dict, *, premium: bool = False) -> str:
    """Строка списка для `.шаб список`: иконка + имя + тип. HTML, экранировано.

    `record` подставляет детали, которые уместны конкретному типу
    (длительность голосового, «с подписью» у медиа и т.п.), чтобы список
    отличал «просто фото» от «фото с подписью».
    """
    ico = icon(kind, premium=premium)
    name = _esc(record.get("name", ""))
    extra = _detail(kind, record)
    suffix = f" <i>· {extra}</i>" if extra else ""
    return f"{ico} <code>{name}</code>{suffix}"


def _detail(kind: str, record: dict) -> str:
    dur = record.get("duration") or 0
    if kind == KIND_VOICE:
        return _fmt_dur(dur)
    if kind in (KIND_VIDEO, KIND_VIDEO_NOTE, KIND_ANIMATION, KIND_AUDIO):
        dim = ""
        if record.get("w") and record.get("h"):
            dim = f"{record['w']}×{record['h']}"
        bits = [b for b in (_fmt_dur(dur) if dur else "", dim) if b]
        return " · ".join(bits)
    if kind in (KIND_PHOTO, KIND_STICKER, KIND_DOCUMENT):
        return "с подписью" if record.get("text") else ""
    if kind == KIND_AUDIO:
        return _esc(record.get("title") or "")
    if kind == KIND_GEO:
        return f"{record.get('lat')}, {record.get('long')}"
    return ""


def _fmt_dur(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    s = int(seconds)
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}" if m else f"{sec}с"


def sort_records(bucket: dict[str, dict]) -> list[dict]:
    """Сортировка для списка: сначала по типу, потом по имени. Pure.

    Порядок по типу, а не по дате: список это «что у меня есть», а не
    «что я последним сохранял» — иначе пагинация прыгает между страницами
    при каждом добавлении.
    """
    return sorted(
        bucket.values(),
        key=lambda r: (KIND_RANK.get(r.get("kind"), 99),
                       str(r.get("name", "")).lower()),
    )
