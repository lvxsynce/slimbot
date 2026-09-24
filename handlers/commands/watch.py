from aiogram import Router, types

from utils.storage import (
    get_chats_for_user,
    add_chat_for_user,
    remove_chat_for_user,
    is_chat_watched,
    session_exists,
    get_photo_settings,
    update_photo_settings,
)
from utils.texts import Texts, render_for_user
from utils.telethon_manager import telethon_manager
from ._base import command_card

router = Router()

WATCH_CMDS = {".watch", ".следить"}
UNWATCH_CMDS = {".unwatch", ".хватит", ".забыть"}
WATCHED_CMDS = {".watched", ".список"}


def _check(cmds: set[str], text: str | None) -> bool:
    return text is not None and text.strip().lower() in cmds


def _auto_enable_photos(user_id: str, chat_id: int, thread_id: int = 0):
    if not session_exists(user_id):
        return
    s = get_photo_settings(user_id)
    key = [chat_id, thread_id]
    if s["enabled"] and s["mode"] == "all":
        return
    if s["enabled"] and s["mode"] == "only_selected":
        if key not in s["exceptions"]:
            s["exceptions"].append(key)
            update_photo_settings(user_id, s)
        return
    if s["enabled"] and s["mode"] == "all_except":
        if key in s["exceptions"]:
            s["exceptions"].remove(key)
            update_photo_settings(user_id, s)
        return
    update_photo_settings(user_id, {"enabled": True, "mode": "only_selected", "exceptions": [key]})


def _disable_photos_for_chat(user_id: str, chat_id: int, thread_id: int = 0):
    if not session_exists(user_id):
        return
    s = get_photo_settings(user_id)
    if not s["enabled"]:
        return
    key = [chat_id, thread_id]
    if s["mode"] == "all":
        update_photo_settings(user_id, {"enabled": True, "mode": "all_except", "exceptions": [key]})
    elif s["mode"] == "all_except":
        if key not in s["exceptions"]:
            s["exceptions"].append(key)
            update_photo_settings(user_id, s)
    elif s["mode"] == "only_selected":
        if key in s["exceptions"]:
            s["exceptions"].remove(key)
            update_photo_settings(user_id, s)


def _photo_note(uid: str) -> str:
    """Возвращает premium-aware хинт о фото-режиме."""
    if session_exists(uid):
        return Texts.Watch.PHOTO_NOTE_ON.render(premium=False)
    return Texts.Watch.PHOTO_NOTE_OFF.render(premium=False)


async def handle_telethon(user_id: str, event, thread_id: int = 0):
    """Telethon-side .watch/.unwatch/.watched. premium-aware через ``render_for_user``."""
    from utils.escape import esc
    text = event.raw_text.strip()
    tokens = text.split(maxsplit=1) if text else []
    head = tokens[0].lower() if tokens else ""
    args = text[len(tokens[0]):].strip() if tokens else ""

    if head in WATCH_CMDS:
        if args:
            entity = await telethon_manager.resolve_entity(user_id, args)
            if not entity:
                await event.edit(
                    command_card("Watch", await render_for_user(user_id, Texts.Watch.WATCH_NOT_FOUND, args=esc(args))),
                    parse_mode="html",
                )
                return
            chat_id = entity.id
            target_thread = 0
            chat_title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)
            if add_chat_for_user(user_id, chat_id, target_thread):
                _auto_enable_photos(user_id, chat_id, target_thread)
                await event.edit(
                    command_card("Watch", await render_for_user(
                        user_id, Texts.Watch.WATCH_OK,
                        label=f"{esc(chat_title)} (весь чат)",
                        photo_note=_photo_note(user_id),
                    )),
                    parse_mode="html",
                )
            else:
                await event.edit(
                    command_card("Watch", await render_for_user(
                        user_id, Texts.Watch.WATCH_ALREADY,
                        label=esc(chat_title),
                    )),
                    parse_mode="html",
                )
        else:
            chat_id = event.chat_id
            target_thread = thread_id
            chat_title = getattr(event.chat, "title", None) or getattr(event.chat, "first_name", None) or f"чат #{chat_id}"
            label = chat_title if not target_thread else f"{chat_title} (топик #{target_thread})"
            if add_chat_for_user(user_id, chat_id, target_thread):
                _auto_enable_photos(user_id, chat_id, target_thread)
                await event.edit(
                    command_card("Watch", await render_for_user(
                        user_id, Texts.Watch.WATCH_OK,
                        label=esc(label),
                        photo_note=_photo_note(user_id),
                    )),
                    parse_mode="html",
                )
            else:
                await event.edit(
                    command_card("Watch", await render_for_user(
                        user_id, Texts.Watch.WATCH_ALREADY,
                        label=esc(label),
                    )),
                    parse_mode="html",
                )

    elif head in UNWATCH_CMDS:
        chat_id = event.chat_id
        target_thread = thread_id
        chat_title = getattr(event.chat, "title", None) or getattr(event.chat, "first_name", None) or f"чат #{chat_id}"
        label = chat_title if not target_thread else f"{chat_title} (топик #{target_thread})"
        if remove_chat_for_user(user_id, chat_id, target_thread):
            _disable_photos_for_chat(user_id, chat_id, target_thread)
            await event.edit(
                command_card("Unwatch", await render_for_user(
                    user_id, Texts.Watch.UNWATCH_OK, label=esc(label),
                )),
                parse_mode="html",
            )
        else:
            await event.edit(
                command_card("Unwatch", await render_for_user(
                    user_id, Texts.Watch.UNWATCH_NOT, label=esc(label),
                )),
                parse_mode="html",
            )

    elif head in WATCHED_CMDS:
        chats = get_chats_for_user(user_id)
        if not chats:
            text_out = Texts.Watch.WATCHED_EMPTY.render(premium=False)
        else:
            client = event.client
            me_id = 0
            try:
                me = await client.get_me()
                me_id = me.id
            except Exception:
                pass
            header = "<b>Slim bot | Watched chats</b>"
            lines = [header]
            for i, entry in enumerate(chats, 1):
                cid, ttid = entry[0], entry[1]
                link, name = await _resolve_chat_link(client, cid, me_id)
                title = name or f"чат #{cid}"
                head_line = f'{i}. <a href="{link}">{esc(title)}</a>'
                if ttid:
                    text_out_entry = f"{head_line} · топик <code>{ttid}</code>"
                else:
                    text_out_entry = f"{head_line} (весь чат)"
                lines.append(text_out_entry)
            text_out = "\n".join(lines)
        await event.edit(command_card("Watched chats", text_out), parse_mode="html")


async def _resolve_chat_link(client, chat_id: int, me_id: int) -> tuple[str, str]:
    """Возвращает (url, display_name) для chat_id."""
    from utils.escape import esc
    from telethon.tl.types import User
    try:
        entity = await client.get_entity(chat_id)
    except Exception:
        return f"tg://user?id={chat_id}", None

    if isinstance(entity, User):
        first = getattr(entity, "first_name", None) or ""
        last = getattr(entity, "last_name", None) or ""
        name = (first + " " + last).strip() or None
        username = getattr(entity, "username", None)
        if username:
            if entity.id == me_id:
                return "https://t.me/SavedMessages", "Избранное"
            return f"https://t.me/{username}", (name or f"@{username}")
        return f"tg://user?id={entity.id}", name

    title = getattr(entity, "title", None) or f"чат #{chat_id}"
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}", title
    raw = int(chat_id)
    if raw < 0:
        s = str(abs(raw))
        if s.startswith("100"):
            s = s[3:]
        return f"https://t.me/c/{s}", title
    return f"tg://user?id={chat_id}", title


def _thread_id_msg(message: types.Message) -> int:
    tid = getattr(message, "message_thread_id", None)
    if tid is None:
        return 0
    try:
        return int(tid)
    except (TypeError, ValueError):
        return 0


def _chat_label(message: types.Message, thread_id: int) -> str:
    from utils.escape import esc
    chat_title = message.chat.title or message.chat.first_name or f"чат #{message.chat.id}"
    label = chat_title if not thread_id else f"{chat_title} (топик #{thread_id})"
    return esc(label)


@router.message(
    lambda msg: bool(msg.text and msg.text.strip()) and msg.text.strip().lower().split()[0] in WATCH_CMDS
)
async def cmd_dot_watch_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    from utils.escape import esc
    user_id = str(message.from_user.id)
    uid = await resolve_effective_uid(message)
    text = message.text.strip()
    parts = text.split(maxsplit=1)
    args = parts[1].strip() if len(parts) > 1 else ""

    if not args:
        chat_id = message.chat.id
        chat_title = message.chat.title or message.chat.first_name or f"чат #{chat_id}"
        if add_chat_for_user(user_id, chat_id, 0):
            _auto_enable_photos(user_id, chat_id, 0)
            await message.reply(
                command_card("Watch", await render_for_user(
                    uid, Texts.Watch.WATCH_OK,
                    label=esc(chat_title), photo_note=_photo_note(user_id),
                ))
            )
        else:
            await message.reply(
                command_card("Watch", await render_for_user(uid, Texts.Watch.WATCH_ALREADY, label=esc(chat_title)))
            )
        return

    if not session_exists(user_id):
        await message.reply(
            command_card("Watch", Texts.Watch.WATCH_NEED_SESSION.render(premium=False))
        )
        return

    entity = await telethon_manager.resolve_entity(user_id, args)
    if not entity:
        await message.reply(
            command_card("Watch", await render_for_user(uid, Texts.Watch.WATCH_NOT_FOUND, args=esc(args)))
        )
        return
    chat_id = entity.id
    chat_title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)
    if add_chat_for_user(user_id, chat_id, 0):
        _auto_enable_photos(user_id, chat_id, 0)
        await message.reply(
            command_card("Watch", await render_for_user(
                uid, Texts.Watch.WATCH_RESOLVED_OK,
                title=esc(chat_title), chat_id=str(chat_id),
                photo_note=_photo_note(user_id),
            ))
        )
    else:
        await message.reply(
            command_card("Watch", await render_for_user(
                uid, Texts.Watch.WATCH_ALREADY,
                label=esc(chat_title),
            ))
        )


@router.message(lambda msg: _check(UNWATCH_CMDS, msg.text))
async def cmd_dot_unwatch_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    user_id = str(message.from_user.id)
    uid = await resolve_effective_uid(message)
    chat_id = message.chat.id
    thread_id = _thread_id_msg(message)
    label = _chat_label(message, thread_id)

    if remove_chat_for_user(user_id, chat_id, thread_id):
        _disable_photos_for_chat(user_id, chat_id, thread_id)
        await message.reply(
            command_card("Unwatch", await render_for_user(uid, Texts.Watch.UNWATCH_OK, label=label))
        )
    else:
        await message.reply(
            command_card("Unwatch", await render_for_user(uid, Texts.Watch.UNWATCH_NOT, label=label))
        )


@router.message(lambda msg: _check(WATCHED_CMDS, msg.text))
async def cmd_dot_watched_private(message: types.Message):
    user_id = str(message.from_user.id)
    await _send_watched_list(message, user_id)


async def _send_watched_list(message: types.Message, user_id: str):
    chats = get_chats_for_user(user_id)
    if not chats:
        text = Texts.Watch.WATCHED_HINT.render(premium=False)
    else:
        header = Texts.Watch.WATCHED_HEADER.render(premium=False)
        lines = [header + "\n"]
        for i, entry in enumerate(chats, 1):
            cid, ttid = entry[0], entry[1]
            if ttid:
                lines.append(f"{i}. <code>{cid}</code> · топик <code>{ttid}</code>")
            else:
                lines.append(f"{i}. <code>{cid}</code> (весь чат)")
        text = "\n".join(lines)

    await message.reply(command_card("Watched chats", text))
