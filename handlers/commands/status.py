from aiogram import Router, types
from aiogram.filters import Command

from utils.storage import session_exists
from utils.texts import Texts, render_for_user
from ._base import command_card, thread_kwargs

router = Router()


@router.message(Command("status"))
async def cmd_status(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    user_id = str(message.from_user.id)
    if session_exists(user_id):
        text = await render_for_user(uid, Texts.Status.CONNECTED)
    else:
        text = await render_for_user(uid, Texts.Status.DISCONNECTED)
    await message.answer(command_card("Status", text), **thread_kwargs(message))
