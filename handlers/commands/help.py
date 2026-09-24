from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from ._base import command_card, format_help, render_help, thread_kwargs
from utils.storage import session_exists

router = Router()


def help_keyboard(has_session: bool = False) -> InlineKeyboardMarkup:
    if has_session:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="[?] Зачем это", callback_data="w1")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="[+] Включить", callback_data="c1")],
        [InlineKeyboardButton(text="[?] Зачем это", callback_data="w1")],
    ])


def _check(text: str | None) -> bool:
    return text and text.strip().lower() in (".help", ".помощь")


@router.message(Command("help", "capabilities"))
async def cmd_help(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    has_ss = message.from_user and session_exists(str(message.from_user.id))
    text = command_card("Help", await render_help(uid, has_ss))
    await message.answer(text, reply_markup=help_keyboard(has_ss), **thread_kwargs(message))


@router.message(lambda msg: _check(msg.text))
async def cmd_dot_help_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    has_ss = message.from_user and session_exists(str(message.from_user.id))
    text = command_card("Help", await render_help(uid, has_ss))
    await message.reply(text, reply_markup=help_keyboard(has_ss), **thread_kwargs(message))
