from datetime import datetime, timezone

from aiogram import Router, types

from ._base import command_card, thread_kwargs

router = Router()


def _check(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    return t in (".ping", ".пинг")


@router.message(lambda msg: _check(msg.text))
async def cmd_ping_private(message: types.Message):
    """`.ping` — общий форматтер карточки (utils/shared_cmd.ping_body).

    Раньше Telethon-путь печатал карточку на 9 строк, а этот — одну строку с
    `Texts.Ping.PING`. Теперь у обеих карточек один набор полей; каждая
    показывает то, что реально может измерить (Bot API против MTProto).
    """
    from utils.premium import resolve_effective_uid
    from utils.shared_cmd import ping_body, ping_from_edits

    uid = await resolve_effective_uid(message)
    edit_rtt = await _measure_edit_rtt(message)
    body = ping_body(ping_from_edits(edit_rtt))
    await message.reply(command_card("Ping", body), **thread_kwargs(message))


async def _measure_edit_rtt(message: types.Message) -> int | None:
    """Задержка между отправкой команды и её обработкой."""
    sent = message.date
    if sent is None:
        return None
    now = datetime.now(timezone.utc)
    dt = sent if sent.tzinfo else sent.replace(tzinfo=timezone.utc)
    return int((now - dt).total_seconds() * 1000)
