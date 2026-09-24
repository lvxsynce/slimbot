from aiogram import Router, types

from ._base import render_ping, thread_kwargs

router = Router()


def _check(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    return t in (".ping", ".пинг")


@router.message(lambda msg: _check(msg.text))
async def cmd_ping_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    text = await render_ping(uid, message.date)
    await message.reply(text, **thread_kwargs(message))
