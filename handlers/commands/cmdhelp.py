"""Перехват `.cmd справка` ДО роутинга команд. Включается до остальных роутеров."""
from aiogram import Router, types

from ._helpdb import is_help_request, HELPS
from ._base import thread_kwargs
from utils.texts import Texts, render_for_user, render_plain
from utils.premium import is_user_premium

router = Router()


def _is(msg: types.Message) -> bool:
    ok, _ = is_help_request(msg.text)
    return ok


def _render_card(key: str, *, premium: bool) -> str:
    """Локальный рендер карточки per-command help с premium-aware эмодзи.

    ``HELPS[key]`` полностью hardcoded в ``_helpdb.py`` (никакого user-input),
    поэтому escape не нужен. Если когда-нибудь в HELPS попадёт динамика —
    добавить ``utils.escape.esc()`` на ``info["syntax"]`` и ``info["desc"]``.
    """
    info = HELPS.get(key)
    if not info:
        return render_plain(Texts.Cmdhelp.NOT_FOUND, key=key)
    title = Texts.Cmdhelp.TITLE.render(premium=premium, syntax=info["syntax"])
    return f"<b>Slim bot | Help</b>\n<blockquote expandable>{title}\n{info['desc']}</blockquote>"


@router.message(_is)
async def cmd_help_private(message: types.Message):
    uid = str(message.from_user.id) if message.from_user else ""
    # В личке с ботом отвечаем сами (Telethon личку с ботом не видит).
    _, key = is_help_request(message.text)
    premium = await is_user_premium(uid) if uid else False
    await message.reply(_render_card(key, premium=premium), parse_mode="html", **thread_kwargs(message))


def _owner_uid(message: types.Message) -> str | None:
    """UID собеседника в личке с ботом."""
    user = getattr(message, "from_user", None)
    if user is not None:
        uid = getattr(user, "id", None)
        if uid is not None:
            return str(uid)
    return None
