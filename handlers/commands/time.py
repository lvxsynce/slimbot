from aiogram import Router, types

from ._base import render_time, format_time, thread_kwargs

router = Router()


def _check(text: str | None) -> bool:
    return text and text.strip().lower() in (".time", ".время")


async def _send(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    return await render_time(uid)


@router.message(lambda msg: _check(msg.text))
async def cmd_time_private(message: types.Message):
    await message.reply(await _send(message), **thread_kwargs(message))
