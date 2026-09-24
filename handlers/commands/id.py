from aiogram import Router, types

from ._base import render_id, format_id, thread_kwargs

router = Router()


def _check(text: str | None) -> bool:
    return text and text.strip().lower() in (".id", ".инфо")


def _tid(message: types.Message) -> int:
    tid = getattr(message, "message_thread_id", None)
    if not tid:
        return 0
    try:
        return int(tid)
    except (TypeError, ValueError):
        return 0


async def _send(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    return await render_id(
        uid,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name or "",
        thread_id=_tid(message) or None,
    )


@router.message(lambda msg: _check(msg.text))
async def cmd_id_private(message: types.Message):
    await message.reply(await _send(message), **thread_kwargs(message))
