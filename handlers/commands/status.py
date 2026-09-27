from aiogram import Router, types
from aiogram.filters import Command

from utils.storage import (
    session_exists,
    get_chats_for_user,
    get_photo_settings,
    get_auto_tr_chats,
    get_nya_chats,
)
from utils.texts import Texts, render_for_user
from ._base import command_card, thread_kwargs

router = Router()


def _fmt_uptime(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} сек"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}м {sec}с"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}ч {minutes}м"
    days, hours = divmod(hours, 24)
    return f"{days}д {hours}ч"


def _photo_line(uid: str) -> str:
    s = get_photo_settings(uid)
    if not isinstance(s, dict) or not s.get("enabled"):
        return "Фото: выключено"
    mode = s.get("mode", "all")
    n = len(s.get("exceptions", []) or [])
    if mode == "only_selected":
        return f"Фото: только выбранные ({n})"
    if mode == "all_except":
        return f"Фото: все, кроме ({n})"
    return "Фото: все чаты"


def _status_lines(user_id: str, has_session: bool, uptime_s: float) -> list[str]:
    """Строки карточки статуса. Pure (только storage) — покрыто тестами."""
    lines = [
        f"Отслеживается чатов: {len(get_chats_for_user(user_id))}",
        _photo_line(user_id),
        f"Авто-перевод: {len(get_auto_tr_chats(user_id))}",
        f"Ня-режим: {len(get_nya_chats(user_id))}",
        f"Аптайм: {_fmt_uptime(uptime_s)}",
    ]
    if not has_session:
        lines.append("Нажми /start → [+] Включить.")
    return lines


@router.message(Command("status"))
async def cmd_status(message: types.Message):
    from utils.premium import resolve_effective_uid
    from handlers.inline import uptime_seconds
    uid = await resolve_effective_uid(message)
    user_id = str(message.from_user.id)
    has_ss = session_exists(user_id)
    session_line = await render_for_user(
        uid, Texts.Status.CONNECTED if has_ss else Texts.Status.DISCONNECTED
    )
    body = session_line + "\n" + "\n".join(_status_lines(user_id, has_ss, uptime_seconds()))
    await message.answer(command_card("Status", body), **thread_kwargs(message))
