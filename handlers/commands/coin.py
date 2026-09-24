"""Команда .монетка / .coin — орёл или решка.
Работает без сессии (aiogram)."""

import random

from aiogram import Router, types

from ._base import thread_kwargs
from utils.texts import Texts, render_for_user

router = Router()

COIN_CMDS = {".монетка", ".coin", ".монета", ".орёл", ".решка"}


def _check(text: str | None) -> bool:
    if not text:
        return False
    head = text.strip().lower().split()[0]
    return head in COIN_CMDS


async def _send(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    result = random.choice(["орёл", "решка"])
    text_obj = Texts.Coin.HEAD if result == "орёл" else Texts.Coin.TAIL
    return await render_for_user(uid, text_obj)


@router.message(lambda msg: _check(msg.text))
async def cmd_coin_private(message: types.Message):
    await message.reply(await _send(message), **thread_kwargs(message))
