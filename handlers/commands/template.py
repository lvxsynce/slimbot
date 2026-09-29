"""`.шаб` — шаблоны сообщений: сохранить реплай и отправлять его позже.

Telethon-only: сохранение и переотправка любых типов медиа (кружок, голосовое,
стикер, контакт, координаты) требуют MTProto — бот не может ни прочитать чужое
сообщение через Bot API, ни отправить `video_note`/`voice` флагом.

Команды:
    .шаб <имя>              — отправить шаблон
    .+шаб <имя>             — сохранить replied сообщение под именем
    .-шаб <имя>             — удалить шаблон
    .шаб список [стр]       — список с пагинацией (`.шаб список 2`)
    .шаб                    — справка

Хранение: метаданные в templates.json (utils/storage.py), тело — файл в
templates/<user_hash>/. Тип сообщения определяет utils/media_kind.py.
"""

from __future__ import annotations

import html as _html
import io
import logging
import time

from utils import media_kind as mk

from ._base import command_card

logger = logging.getLogger(__name__)

BASE_CMDS = (".шаб", ".шаблон", ".template", ".tmpl", ".tpl")
ADD_CMDS = (".+шаб", ".+шаблон", ".+template", ".+tmpl", ".+tpl")
DEL_CMDS = (".-шаб", ".-шаблон", ".-template", ".-tmpl", ".-tpl")
ALL_CMDS = BASE_CMDS + ADD_CMDS + DEL_CMDS

#: Зарезервированные имена: иначе `.шаб список` стал бы неоднозначным.
RESERVED = frozenset({"список", "list", "все", "all"})
LIST_WORDS = frozenset({"список", "list", "ls"})

#: Типы, для которых Telegram не умеет caption — при отправке подпись теряется.
NO_CAPTION_KINDS = frozenset({mk.KIND_VOICE, mk.KIND_VIDEO_NOTE})


def _esc(s) -> str:
    return _html.escape(str(s if s is not None else ""), quote=False)


def _fmt_size(n) -> str:
    """Человекочитаемый размер: `0 Б` / `812 КБ` / `47 МБ` / `1.0 ГБ`."""
    n = int(n or 0)
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} КБ"
    if n < 1024 ** 3:
        return f"{n / (1024 * 1024):.0f} МБ"
    return f"{n / (1024 ** 3):.1f} ГБ"


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русская форма числительного: 1 шаблон / 2 шаблона / 5 шаблонов."""
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _card(body: str) -> str:
    return command_card("Шаблоны", body)


def _limit_card(what: str, name: str, lines: list[str]) -> str:
    """Карточка отказа по лимиту: что не получилось, почему и что делать.

    Юзер не может увеличить лимит сам, поэтому обязательно показываем
    конкретный следующий шаг (удалить лишнее) и имя команды, которой это
    делается — иначе сообщение выглядит как глухой отказ.
    """
    return _card("\n".join([
        f"[x] Не сохраню <b>{_esc(name)}</b>",
        f"Причина: {what}",
        "",
        *lines,
    ]))


def _cfg() -> dict:
    from config import (
        TEMPLATE_LIST_PAGE_SIZE, TEMPLATE_MAX_BYTES, TEMPLATE_MAX_COUNT,
        TEMPLATE_MAX_TOTAL_BYTES, TEMPLATE_NAME_MAX_LEN,
    )
    return {
        "page": max(1, int(TEMPLATE_LIST_PAGE_SIZE)),
        "max_bytes": int(TEMPLATE_MAX_BYTES),
        "max_count": int(TEMPLATE_MAX_COUNT),
        "max_total": int(TEMPLATE_MAX_TOTAL_BYTES),
        "name_max": int(TEMPLATE_NAME_MAX_LEN),
    }


async def _premium(client) -> bool:
    """True если юзер Premium И нужные custom-emoji id ещё живы.

    Обе проверки нужны: без Premium Telegram всё равно нарисует fallback,
    а без валидации id в разметке окажется мёртвый emoji-id.
    """
    if client is None:
        return False
    try:
        from utils.premium import is_user_premium
        me = await client.get_me()
        uid = getattr(me, "id", None)
        if uid is None or not await is_user_premium(uid):
            return False
        return bool(await mk.resolve_custom_emoji(client))
    except Exception as e:
        logger.debug("template: premium probe failed: %s", e)
        return False


def _avail_lines(bucket: dict, premium: bool, limit: int = 8) -> str:
    records = mk.sort_records(bucket)
    if not records:
        return "<i>— пусто —</i>"
    return "\n".join(
        mk.describe(r.get("kind"), r, premium=premium) for r in records[:limit]
    )


# ----------------- .шаб список -----------------

async def _cmd_list(user_id: str, event, args: str) -> None:
    from utils import storage

    cfg = _cfg()
    page_size = cfg["page"]
    page = 1
    if args:
        token = args.split()[0]
        if not token.isdigit():
            await event.edit(_card(
                f"[x] <code>{_esc(token)}</code> — это не номер страницы.\n"
                f"Пример: <code>.шаб список 2</code>"
            ), parse_mode="html")
            return
        page = int(token)

    records = mk.sort_records(storage.get_templates(user_id))
    if not records:
        await event.edit(_card(
            "[?] Шаблонов пока нет.\n"
            "Ответь на любое сообщение и сохрани его: <code>.+шаб привет</code>"
        ), parse_mode="html")
        return

    pages = max(1, (len(records) + page_size - 1) // page_size)
    if page < 1 or page > pages:
        await event.edit(_card(
            f"[x] Страницы {page} нет — всего <b>{pages}</b>.\n"
            f"Последняя: <code>.шаб список {pages}</code>"
        ), parse_mode="html")
        return

    start = (page - 1) * page_size
    premium = await _premium(event.client)
    lines = [
        mk.describe(r.get("kind"), r, premium=premium)
        for r in records[start:start + page_size]
    ]

    nav = [f"<b>стр. {page}/{pages}</b>"]
    if page > 1:
        nav.append(f"← <code>.шаб список {page - 1}</code>")
    if page < pages:
        nav.append(f"→ <code>.шаб список {page + 1}</code>")
    lines.append(" · ".join(nav))
    lines.append(f"<i>всего: {len(records)}</i>")
    await event.edit(_card("\n".join(lines)), parse_mode="html")


# ----------------- .+шаб -----------------

async def _cmd_add(user_id: str, event, args: str) -> None:
    from utils import storage

    cfg = _cfg()
    name = " ".join(str(args or "").split())
    if not name:
        await event.edit(_card(
            "[?] Нужно имя шаблона.\n"
            "Ответь на сообщение и сохрани: <code>.+шаб привет</code>"
        ), parse_mode="html")
        return
    if len(name) > cfg["name_max"]:
        await event.edit(_card(
            f"[x] Имя длиннее {cfg['name_max']} символов (сейчас {len(name)})."
        ), parse_mode="html")
        return
    if storage.normalize_template_name(name) in RESERVED:
        await event.edit(_card(
            f"[x] <code>{_esc(name)}</code> — зарезервированное имя.\n"
            f"Занято: <code>{', '.join(sorted(RESERVED))}</code>"
        ), parse_mode="html")
        return

    reply = await event.get_reply_message()
    if not reply:
        await event.edit(_card(
            "[?] Нужно ответить на сообщение — оно и станет шаблоном.\n"
            "Текст, фото, видео, кружок, голосовое, стикер, контакт — всё подходит."
        ), parse_mode="html")
        return

    kind, meta = mk.classify(reply)
    spec = mk.KINDS.get(kind, mk.KINDS[mk.KIND_TEXT])
    bucket = storage.get_templates(user_id)
    key = storage.normalize_template_name(name)
    old = bucket.get(key)
    is_new = old is None

    if is_new and len(bucket) >= cfg["max_count"]:
        pages = max(1, (len(bucket) + cfg["page"] - 1) // cfg["page"])
        await event.edit(_limit_card(
            f"уже {cfg['max_count']} "
            f"{_plural(cfg['max_count'], 'шаблон', 'шаблона', 'шаблонов')}"
            f" — это максимум",
            name,
            [
                f"Удалите ненужное: <code>.шаб список</code> "
                f"({pages} {_plural(pages, 'страница', 'страницы', 'страниц')}), "
                f"затем <code>.-шаб имя</code>.",
                f"<i>Лимит настраивается через .env: "
                f"TEMPLATE_MAX_COUNT</i>",
            ],
        ), parse_mode="html")
        return

    size = int(meta.get("size") or 0)
    blob = None
    if spec.has_file:
        if size > cfg["max_bytes"]:
            await event.edit(_limit_card(
                f"файл {_fmt_size(size)}, а лимит на один шаблон — "
                f"{_fmt_size(cfg['max_bytes'])}",
                name,
                [
                    "Сожмите файл (например, обрежьте видео) и сохраните снова.",
                    "<i>Лимит настраивается через .env: TEMPLATE_MAX_BYTES</i>",
                ],
            ), parse_mode="html")
            return
        used = storage.templates_total_bytes(user_id)
        free = cfg["max_total"] - used
        if old:
            # Перезапись: старый файл освободит место.
            free += int(old.get("size") or 0)
        if size > free:
            await event.edit(_limit_card(
                f"занято {_fmt_size(used)} из {_fmt_size(cfg['max_total'])}, "
                f"а файл занимает ещё {_fmt_size(size)}",
                name,
                [
                    "Освободите место: <code>.шаб список</code>, затем "
                    "<code>.-шаб имя</code>.",
                    "<i>Лимит настраивается через .env: "
                    "TEMPLATE_MAX_TOTAL_BYTES</i>",
                ],
            ), parse_mode="html")
            return
        try:
            data = await reply.download_media(file=bytes)
        except Exception as e:
            logger.exception("template: download failed: %s", type(e).__name__)
            await event.edit(_card(
                f"[x] Не скачался файл: <code>{_esc(type(e).__name__)}</code>\n"
                f"<i>{_esc(str(e)[:200])}</i>"
            ), parse_mode="html")
            return
        if not data:
            await event.edit(_card(
                "[x] Файл пустой — сохранять нечего."
            ), parse_mode="html")
            return
        size = len(data)
        blob = storage.write_template_blob(
            user_id, name, data, meta.get("ext") or ".bin")

    record = dict(meta)
    record["kind"] = kind
    record["size"] = size
    record["created"] = int(time.time())
    if blob:
        record["file"] = blob
    else:
        record.pop("file", None)
    storage.put_template(user_id, name, record)

    if old and old.get("file") and old.get("file") != blob:
        storage.drop_template_blob(user_id, old)

    premium = await _premium(event.client)
    lines = [
        f"{mk.icon(kind, premium=premium)} <b>{_esc(name)}</b> — "
        f"{spec.label.lower()}",
        f"[+] Шаблон {'перезаписан' if old else 'сохранён'}",
    ]
    if size:
        lines.append(f"размер: {_fmt_size(size)}")
    if record.get("text"):
        preview = " ".join(str(record["text"]).split())[:60]
        lines.append(f"подпись: <i>{_esc(preview)}</i>")
    if kind in NO_CAPTION_KINDS and record.get("text"):
        lines.append(
            "<i>Telegram не умеет подпись к такому типу — при отправке "
            "подпись не поедет.</i>"
        )
    lines.append(f"отправить: <code>.шаб {_esc(name)}</code>")
    # Показываем расход квоты при каждом сохранении: юзер видит предел
    # ДО того, как упрётся в отказ, а не после.
    bucket_now = storage.get_templates(user_id)
    lines.append(
        f"<i>шаблонов: {len(bucket_now)}/{cfg['max_count']} · "
        f"занято: {_fmt_size(storage.templates_total_bytes(user_id))}"
        f"/{_fmt_size(cfg['max_total'])}</i>"
    )
    await event.edit(_card("\n".join(lines)), parse_mode="html")


# ----------------- .-шаб -----------------

async def _cmd_del(user_id: str, event, args: str) -> None:
    from utils import storage

    name = " ".join(str(args or "").split())
    if not name:
        bucket = storage.get_templates(user_id)
        premium = await _premium(event.client)
        await event.edit(_card(
            "[?] Нужно имя шаблона.\n"
            f"Список: <code>.шаб список</code>\n\n"
            f"Сейчас сохранено:\n{_avail_lines(bucket, premium)}"
        ), parse_mode="html")
        return

    rec = storage.delete_template(user_id, name)
    if not rec:
        premium = await _premium(event.client)
        bucket = storage.get_templates(user_id)
        await event.edit(_card(
            f"[x] Шаблон <code>{_esc(name)}</code> не найден.\n\n"
            f"Есть:\n{_avail_lines(bucket, premium)}"
        ), parse_mode="html")
        return

    await event.edit(_card(
        f"[+] Удалён шаблон <code>{_esc(rec.get('name', name))}</code> "
        f"({mk.label(rec.get('kind'))})."
    ), parse_mode="html")


# ----------------- .шаб <имя> -----------------

async def _cmd_send(user_id: str, event, args: str) -> None:
    from utils import storage
    from utils.telethon_manager import telethon_reply_to

    name = " ".join(str(args or "").split())
    rec = storage.get_template(user_id, name)
    if not rec:
        premium = await _premium(event.client)
        bucket = storage.get_templates(user_id)
        hint = f"Список: <code>.шаб список</code>" if bucket else \
            "Пока пусто — сохрани реплай: <code>.+шаб привет</code>"
        await event.edit(_card(
            f"[x] Шаблон <code>{_esc(name)}</code> не найден.\n\n"
            f"Есть:\n{_avail_lines(bucket, premium)}\n\n{hint}"
        ), parse_mode="html")
        return

    try:
        # ВАЖНО: вызываем telethon_reply_to(event), а не передаём саму функцию.
        # Раньше здесь стоял `telethon_reply_to` — и reply_to уезжал в сеть
        # объектом-функцией, на что Telethon отвечал
        # «TypeError: Invalid message type: <class 'function'>».
        sent = await _send_record(user_id, event, rec, telethon_reply_to(event))
    except Exception as e:
        logger.exception("template: send failed: %s", type(e).__name__)
        await event.edit(_card(
            f"[x] Не отправилось: <code>{_esc(type(e).__name__)}</code>\n"
            f"<i>{_esc(str(e)[:200])}</i>"
        ), parse_mode="html")
        return
    if not sent:
        await event.edit(_card(
            f"[x] Файл шаблона <code>{_esc(rec.get('name', name))}</code> "
            f"потерян — сохрани заново: <code>.+шаб {_esc(name)}</code>"
        ), parse_mode="html")
        return
    # Команда свою функцию выполнила — убираем её, как .q / .del.
    try:
        await event.delete()
    except Exception as e:
        logger.debug("template: delete command failed: %s", e)


async def _send_record(user_id: str, event, rec: dict, reply_to) -> bool:
    """Отправляет сохранённый шаблон. False — файл потерян или пуст."""
    from utils import storage

    client = event.client
    chat = event.chat_id
    kind = rec.get("kind") or mk.KIND_TEXT
    text = str(rec.get("text") or "")
    caption = text or None

    if kind == mk.KIND_TEXT:
        await client.send_message(chat, text or ".", reply_to=reply_to)
        return True

    if kind == mk.KIND_CONTACT:
        # InputMediaContact принимает только 4 поля (user_id есть у
        # MessageMediaContact на выходе, но не у input-типа).
        from telethon.tl.types import InputMediaContact
        await client.send_message(
            chat,
            file=InputMediaContact(
                phone_number=rec.get("phone") or "",
                first_name=rec.get("first_name") or "",
                last_name=rec.get("last_name") or "",
                vcard=rec.get("vcard") or "",
            ),
            reply_to=reply_to,
        )
        return True

    if kind == mk.KIND_GEO:
        # Поле называется geo_point, а не geo.
        from telethon.tl.types import InputGeoPoint, InputMediaGeoPoint
        await client.send_message(
            chat,
            file=InputMediaGeoPoint(
                geo_point=InputGeoPoint(
                    lat=float(rec.get("lat") or 0.0),
                    long=float(rec.get("long") or 0.0),
                ),
            ),
            reply_to=reply_to,
        )
        return True

    data = storage.read_template_blob(user_id, rec)
    if not data:
        return False

    ext = rec.get("ext") or ".bin"
    mime = rec.get("mime") or None
    dur = int(rec.get("duration") or 0)
    w = int(rec.get("w") or 0)
    h = int(rec.get("h") or 0)

    def buf():
        b = io.BytesIO(data)
        b.name = "template" + ext
        return b

    if kind == mk.KIND_PHOTO:
        await client.send_file(chat, file=buf(), caption=caption,
                               reply_to=reply_to, parse_mode=None,
                               force_document=False)
    elif kind == mk.KIND_ANIMATION:
        from telethon.tl.types import DocumentAttributeAnimated
        await client.send_file(chat, file=buf(), caption=caption,
                               reply_to=reply_to, parse_mode=None,
                               force_document=False,
                               mime_type=mime or "image/gif",
                               attributes=[DocumentAttributeAnimated()])
    elif kind == mk.KIND_VIDEO:
        from telethon.tl.types import DocumentAttributeVideo
        await client.send_file(chat, file=buf(), caption=caption,
                               reply_to=reply_to, parse_mode=None,
                               force_document=False, supports_streaming=True,
                               attributes=[DocumentAttributeVideo(
                                   duration=dur or 1, w=w or 1, h=h or 1,
                                   round_message=False, supports_streaming=True)])
    elif kind == mk.KIND_VIDEO_NOTE:
        # send_file сам ставит DocumentAttributeVideo(round_message=True).
        await client.send_file(chat, file=buf(), reply_to=reply_to,
                               video_note=True)
    elif kind == mk.KIND_VOICE:
        # voice_note=True → DocumentAttributeAudio(voice=True). Caption
        # Telegram не принимает, поэтому текст (если был) не пересылаем.
        await client.send_file(chat, file=buf(), reply_to=reply_to,
                               voice_note=True)
    elif kind == mk.KIND_AUDIO:
        from telethon.tl.types import DocumentAttributeAudio
        await client.send_file(chat, file=buf(), caption=caption,
                               reply_to=reply_to, parse_mode=None,
                               force_document=False,
                               attributes=[DocumentAttributeAudio(
                                   duration=dur, voice=False,
                                   title=rec.get("title"),
                                   performer=rec.get("performer"))])
    elif kind == mk.KIND_STICKER:
        await _send_sticker(client, chat, data, mime, ext, caption, reply_to)
    else:
        # KIND_DOCUMENT и всё, что не распознали.
        await client.send_file(chat, file=buf(), caption=caption,
                               reply_to=reply_to, parse_mode=None,
                               force_document=True)
    return True


async def _send_sticker(client, chat, data, mime, ext, caption, reply_to) -> None:
    """Стикер: пробуем отправить стикером, иначе — файлом.

    Telegram принимает перезаливку webp/tgs с DocumentAttributeSticker и пустым
    sticker-set (так же делает resolve_bot_file_id в Telethon), но не всегда.
    Фолбэк на force_document гарантирует, что юзер увидит хоть что-то.
    """
    from telethon.tl.types import DocumentAttributeSticker, InputStickerSetEmpty
    b = io.BytesIO(data)
    b.name = "template" + ext
    try:
        await client.send_file(chat, file=b, reply_to=reply_to, parse_mode=None,
                               force_document=False,
                               mime_type=mime or "image/webp",
                               attributes=[DocumentAttributeSticker(
                                   alt="", stickerset=InputStickerSetEmpty())])
        return
    except Exception as e:
        logger.debug("template: sticker upload as sticker failed (%s), "
                     "falling back to file", e)
    b2 = io.BytesIO(data)
    b2.name = "template" + ext
    await client.send_file(chat, file=b2, caption=caption, reply_to=reply_to,
                           parse_mode=None, force_document=True)


# ----------------- справка / диспетчер -----------------

#: Пример имени шаблона для каждого типа (для .шаб справки).
_EXAMPLE = {
    mk.KIND_TEXT: "привет",
    mk.KIND_PHOTO: "обложка",
    mk.KIND_VIDEO: "ролик",
    mk.KIND_ANIMATION: "гифка",
    mk.KIND_VIDEO_NOTE: "поздравление",
    mk.KIND_VOICE: "аудио",
    mk.KIND_AUDIO: "трек",
    mk.KIND_STICKER: "наклейка",
    mk.KIND_DOCUMENT: "файл",
    mk.KIND_CONTACT: "визитка",
    mk.KIND_GEO: "точка",
}


def help_card() -> str:
    rows = "\n".join(
        f"{mk.icon(k)} <code>.шаб {_esc(_EXAMPLE[k])}</code> — {mk.label(k).lower()}"
        for k in mk.KIND_ORDER if k in _EXAMPLE
    )
    return _card(
        "Шаблоны сообщений: сохрани сообщение и отправляй потом одним словом.\n\n"
        "<b>Команды:</b>\n"
        "<code>.шаб имя</code> — отправить шаблон\n"
        "<code>.+шаб имя</code> — сохранить сообщение, на которое ответил\n"
        "<code>.-шаб имя</code> — удалить шаблон\n"
        "<code>.шаб список [стр]</code> — список с пагинацией\n"
        "<code>.шаб</code> — эта справка\n\n"
        f"<b>Типы:</b>\n{rows}\n\n"
        "<i>Только с Telethon-сессией.</i>"
    )


async def handle(user_id: str, event) -> None:
    """Telethon-вызов из telethon_manager._handle_outgoing."""
    text = (event.raw_text or "").strip()
    head, _, args = text.partition(" ")
    head = head.lower()
    args = args.strip()

    if head in ADD_CMDS:
        await _cmd_add(user_id, event, args)
    elif head in DEL_CMDS:
        await _cmd_del(user_id, event, args)
    elif head in BASE_CMDS:
        if not args:
            await event.edit(help_card(), parse_mode="html")
            return
        if args.split()[0].lower() in LIST_WORDS:
            await _cmd_list(user_id, event, " ".join(args.split()[1:]))
            return
        await _cmd_send(user_id, event, args)
    else:  # pragma: no cover — вызов не из диспетчера
        await event.edit(_card("[x] Неизвестная команда."), parse_mode="html")
