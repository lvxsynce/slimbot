from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from utils.storage import session_exists
from utils.telethon_manager import telethon_manager
from utils.texts import Texts, render_for_user, render_plain
from ._base import command_card, thread_kwargs

router = Router()


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="[x] Выйти", callback_data="logout_yes")],
        [InlineKeyboardButton(text="[<-] Отмена", callback_data="logout_no")],
    ])


@router.message(Command("logout"))
async def cmd_logout(message: types.Message):
    uid = str(message.from_user.id)
    if not session_exists(uid):
        await message.answer(
            command_card("Logout", render_plain(Texts.Logout.NOT_NEEDED)),
            **thread_kwargs(message),
        )
        return
    from utils.premium import resolve_effective_uid
    eff_uid = await resolve_effective_uid(message)
    text = command_card("Logout", await render_for_user(eff_uid, Texts.Logout.CONFIRM))
    await message.answer(text, reply_markup=_confirm_kb(), **thread_kwargs(message))


@router.callback_query(F.data == "logout_ask")
async def logout_ask_cb(callback: types.CallbackQuery):
    from utils.premium import resolve_effective_uid
    uid = str(callback.from_user.id)
    if not session_exists(uid):
        await callback.answer("Уже выключено", show_alert=False)
        return
    eff_uid = await resolve_effective_uid(callback.message)
    text = command_card("Logout", await render_for_user(eff_uid, Texts.Logout.CONFIRM))
    try:
        await callback.message.edit_text(text, reply_markup=_confirm_kb())
    except Exception:
        await callback.message.answer(text, reply_markup=_confirm_kb())
    await callback.answer()


@router.callback_query(F.data == "logout_no")
async def logout_no_cb(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(command_card("Logout", render_plain(Texts.Logout.CANCELLED)))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "logout_yes")
async def logout_yes_cb(callback: types.CallbackQuery):
    uid = str(callback.from_user.id)
    if not session_exists(uid):
        await callback.answer("Уже выключено", show_alert=False)
        try:
            await callback.message.edit_text(command_card("Logout", render_plain(Texts.Logout.ALREADY_OFF)))
        except Exception:
            pass
        return
    await callback.answer("Отключаю...")
    revoked = await telethon_manager.logout(uid)
    note = (
        render_plain(Texts.Logout.NOTE_REVOKED) if revoked
        else render_plain(Texts.Logout.NOTE_LOCAL)
    )
    text = command_card("Logout", render_plain(Texts.Logout.DONE, note=note))
    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)
