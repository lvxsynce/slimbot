"""Dot-команды: .me/.я, .chat/.чат, .who/.кто для aiogram-пути
(приватный чат с ботом).

Telethon-путь с возможностью резолва чужих профилей живёт в
utils.telethon_manager.py::_handle_outgoing.
"""

from aiogram import Router, types

from utils.texts import Texts, render_for_user, render_plain
from ._base import command_card, thread_kwargs


router = Router()


def _check_me(text: str | None) -> bool:
    return text and text.strip().lower() in (".me", ".я")


def _check_chat(text: str | None) -> bool:
    return text and text.strip().lower() in (".chat", ".чат")


def _check_who(text: str | None) -> bool:
    return text and text.strip().lower() in (".who", ".кто")


def _bool_yn(v) -> str:
    return "да" if v else "нет"


def _active_usernames(obj) -> list[str]:
    """Извлекает активные usernames из aiogram User/Chat (Bot API 7.5+)."""
    out: list[str] = []
    raw = getattr(obj, "usernames", None)
    if raw:
        for u in raw:
            uname = getattr(u, "username", None)
            active = getattr(u, "active", False)
            if uname and active and uname not in out:
                out.append(uname)
    if not out:
        legacy = getattr(obj, "username", None)
        if legacy:
            out.append(legacy)
    return out


def _render_usernames(unames: list[str]) -> str:
    """'Username: @a' (один) или 'Usernames: @a @b' (несколько)."""
    from utils.escape import esc
    if not unames:
        return ""
    if len(unames) == 1:
        return f"Юзернейм: @{esc(unames[0])}"
    return "Юзернеймы: " + " ".join(f"@{esc(u)}" for u in unames)


async def _make_me(uid, u: types.User) -> str:
    """Рендерит карточку .me с premium-aware эмодзи в заголовке."""
    from utils.escape import esc
    title = "<b>Slim bot | Me</b>"
    lines = [title, f"ID: <code>{u.id}</code>", f"Имя: {esc(u.first_name or '-')}"]
    if u.last_name:
        lines.append(f"Фамилия: {esc(u.last_name)}")
    unames = _active_usernames(u)
    if unames:
        lines.append(_render_usernames(unames))
    if u.language_code:
        lines.append(f"Язык: <code>{u.language_code}</code>")
    if getattr(u, "is_premium", False):
        lines.append(Texts.Me.PREMIUM_YES.render(premium=False))
    return command_card("Me", "\n".join(lines[1:]))


def _make_chat(c: types.Chat, thread_id: int = 0) -> str:
    """Рендер карточки .chat. Эмодзи фиксированный (юзер не выбирал)."""
    from utils.escape import esc
    title = "<b>Slim bot | Chat</b>"
    lines = [title, f"ID: <code>{c.id}</code>", f"Тип: {c.type}"]
    ct = c.title or c.first_name
    if ct:
        lines.append(f"Название: {esc(ct)}")
    unames = _active_usernames(c)
    if unames:
        lines.append(_render_usernames(unames))
    if thread_id:
        lines.append(f"Топик: <code>{thread_id}</code>")
    return command_card("Chat", "\n".join(lines[1:]))


async def _make_who_aiogram(uid, u: types.User) -> str:
    """Рендер карточки .who (Bot API-поля: language/premium/bot-флаги)."""
    from utils.escape import esc
    title = "<b>Slim bot | Who</b>"
    lines = [title, f"ID: <code>{u.id}</code>", f"Имя: {esc(u.first_name or '-')}"]
    if u.last_name:
        lines.append(f"Фамилия: {esc(u.last_name)}")
    unames = _active_usernames(u)
    if unames:
        lines.append(_render_usernames(unames))
    if u.language_code:
        lines.append(f"Язык: <code>{u.language_code}</code>")
    lines.append(f"Бот: {_bool_yn(u.is_bot)}")
    if getattr(u, "is_premium", False):
        lines.append(Texts.Me.PREMIUM_YES.render(premium=False))
    if getattr(u, "added_to_attachment_menu", False):
        lines.append("Прикреплён в меню: да")
    if u.is_bot:
        if getattr(u, "can_join_groups", None) is not None:
            lines.append(f"Может в группы: {_bool_yn(u.can_join_groups)}")
        if getattr(u, "can_read_all_group_messages", None) is not None:
            lines.append(f"Видит все сообщения: {_bool_yn(u.can_read_all_group_messages)}")
        if getattr(u, "supports_inline_queries", None) is not None:
            lines.append(f"Inline: {_bool_yn(u.supports_inline_queries)}")
    return command_card("Who", "\n".join(lines[1:]))


def _tid(message: types.Message) -> int:
    t = getattr(message, "message_thread_id", None)
    if not t:
        return 0
    try:
        return int(t)
    except (TypeError, ValueError):
        return 0


@router.message(lambda msg: _check_me(msg.text))
async def cmd_me_private(message: types.Message):
    uid = str(message.from_user.id)
    await message.reply(await _make_me(uid, message.from_user), **thread_kwargs(message))


@router.message(lambda msg: _check_chat(msg.text))
async def cmd_chat_private(message: types.Message):
    uid = str(message.from_user.id)
    await message.reply(_make_chat(message.chat, _tid(message)), **thread_kwargs(message))


@router.message(lambda msg: _check_who(msg.text))
async def cmd_who_private(message: types.Message):
    uid = str(message.from_user.id)
    if not message.reply_to_message:
        await message.reply(
            command_card("Who", Texts.Who.NO_REPLY.render(premium=False)),
            **thread_kwargs(message),
        )
        return
    r = message.reply_to_message
    u = r.from_user
    if not u:
        await message.reply(
            command_card("Who", Texts.Who.NO_SENDER.render(premium=False)),
            **thread_kwargs(message),
        )
        return
    await message.reply(await _make_who_aiogram(uid, u), **thread_kwargs(message))
