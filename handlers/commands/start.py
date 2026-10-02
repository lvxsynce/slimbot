from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import BOT_NAME
import config as cfg
from utils.inline_kb import with_inline_hint
from utils.storage import session_exists
from utils.texts import Texts, render_for_user
from ._base import command_card, thread_kwargs

router = Router()


def _session_kb(has_session: bool, *, may_connect: bool = True) -> InlineKeyboardMarkup:
    """Клавиатура /start.

    ``may_connect=False`` убирает кнопку «Включить» для тех, кому закрыт
    доступ по SESSION_ALLOWLIST: раньше кнопка была, и пользователь узнавал
    об отказе только после нажатия.

    Последняя строка — inline-подсказка: по нажатию юзер выбирает любой чат,
    и inline-режим открывается в нём. Без неё `@bot` приходится вспоминать.
    """
    btns = []
    if has_session:
        btns.append([InlineKeyboardButton(text="[x] Выключить", callback_data="logout_ask")])
    elif may_connect:
        btns.append([InlineKeyboardButton(text="[+] Включить", callback_data="c1")])
    btns.append([InlineKeyboardButton(text="[?] Зачем это", callback_data="w1")])
    return with_inline_hint(InlineKeyboardMarkup(inline_keyboard=btns))


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    from utils.premium import resolve_effective_uid
    from handlers.session import session_allowed
    uid = await resolve_effective_uid(message)
    user_id = str(message.from_user.id)
    has_ss = session_exists(user_id)
    may_connect = session_allowed(user_id)
    username = cfg.BOT_USERNAME or BOT_NAME
    if has_ss:
        ss_status = Texts.Start.SS_ON.render(premium=False)
    elif not may_connect:
        ss_status = "[x] Подключение недоступно"
    else:
        ss_status = Texts.Start.SS_OFF.render(premium=False)

    text = await render_for_user(
        uid, Texts.Start.CONNECTED_GREETING,
        bot_name=BOT_NAME, username=username, ss_status=ss_status,
    )
    await message.answer(
        command_card("Start", text),
        reply_markup=_session_kb(has_ss, may_connect=may_connect),
        **thread_kwargs(message),
    )
