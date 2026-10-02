"""Команда `.timezone [значение]` — настройка часовой зоны для `.time`."""
from html import escape as _h

from aiogram import Router, types

from utils.storage import get_user_tz, set_user_tz
from utils.timezones import is_reset_value
from utils.texts import Texts, render_for_user
from utils.shared_cmd import timezone_body, timezone_parse
from ._base import command_card, thread_kwargs

router = Router()

TZ_CMDS = (".timezone", ".таймзона", ".tz")


def _check(text: str | None) -> bool:
    if not text:
        return False
    head = text.strip().lower().split()[0]
    return head in TZ_CMDS


def _args(text: str) -> str:
    parts = (text or "").strip().split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


async def _status_text(uid: str) -> str:
    """Карточка `.timezone` без аргументов.

    Тело — из `utils/shared_cmd.timezone_body` (общее с Telethon-путём);
    сюда передаётся только premium-заголовок из `Texts.Timezone.TITLE`.
    """
    title = await render_for_user(uid, Texts.Timezone.TITLE)
    return command_card("Timezone", timezone_body(get_user_tz(uid), title=title))


async def _do_timezone(uid: str, args: str) -> str:
    if is_reset_value(args):
        set_user_tz(uid, None)
        return Texts.Timezone.RESET_OK.render(premium=False)
    if not args:
        return await _status_text(uid)
    canonical = _canonicalize(args)
    if canonical is None:
        return await render_for_user(uid, Texts.Timezone.BAD, args=_h(args.strip()))
    set_user_tz(uid, canonical)
    ru_match = next(
        (alias for alias, val in RU_ALIASES.items() if val == canonical),
        None,
    )
    if canonical.lstrip("+-").isdigit():
        sign = "+" if not canonical.startswith("-") else ""
        label = f"UTC{sign}{canonical.lstrip('+-')}"
        return await render_for_user(uid, Texts.Timezone.SET_OFFSET, label=label)
    if ru_match:
        return await render_for_user(uid, Texts.Timezone.SET_ALIAS, canonical=canonical, alias=ru_match)
    return await render_for_user(uid, Texts.Timezone.SET_IANA, canonical=canonical)


@router.message(lambda m: _check(m.text))
async def cmd_timezone_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    eff_uid = await resolve_effective_uid(message)
    await message.reply(
        await _do_timezone(eff_uid, _args(message.text or "")),
        **thread_kwargs(message),
    )
