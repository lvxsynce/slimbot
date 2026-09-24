from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import BOT_NAME
import config as cfg
from utils.storage import session_exists
from utils.texts import Texts, render_for_user
from ._base import command_card, thread_kwargs

router = Router()


def _session_kb(has_session: bool) -> InlineKeyboardMarkup:
    btns = []
    if has_session:
        btns.append([InlineKeyboardButton(text="[x] Выключить", callback_data="logout_ask")])
    else:
        btns.append([InlineKeyboardButton(text="[+] Включить", callback_data="c1")])
    btns.append([InlineKeyboardButton(text="[?] Зачем это", callback_data="w1")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    user_id = str(message.from_user.id)
    has_ss = session_exists(user_id)
    username = cfg.BOT_USERNAME or BOT_NAME
    ss_status = (
        Texts.Start.SS_ON.render(premium=False) if has_ss
        else Texts.Start.SS_OFF.render(premium=False)
    )

    text = await render_for_user(
        uid, Texts.Start.CONNECTED_GREETING,
        bot_name=BOT_NAME, username=username, ss_status=ss_status,
    )
    await message.answer(command_card("Start", text), reply_markup=_session_kb(has_ss), **thread_kwargs(message))
