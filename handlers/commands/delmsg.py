"""Команда .del / .удалить — удалить N своих сообщений. Telethon-only."""

import asyncio

from telethon.errors import FloodWaitError, MessageDeleteForbiddenError

from utils.texts import Texts, render_for_user
from handlers.commands._base import command_card

DEL_CMDS = (".del", ".удалить")


def _check(t: str | None) -> bool:
    if not t:
        return False
    head = t.strip().lower().split()[0]
    return head in DEL_CMDS


async def handle(user_id: str, event) -> None:
    """Telethon-вызов из telethon_manager._handle_outgoing."""
    from html import escape as _h
    text = (event.raw_text or "").strip()
    parts = text.split(maxsplit=1)
    args = parts[1].strip() if len(parts) > 1 else ""
    n = 1
    if args:
        head = args.split()[0]
        if head.lstrip("+-").isdigit():
            n = max(1, min(int(head), 100))

    # Команда сама (`event.id`) ВСЕГДА попадает в список удаления — `to_del = [event.id]`
    # — и потом добирается через iter_messages для остальных N.
    try:
        msgs = []
        me = await event.client.get_me()
        # Команда первая
        msgs.append(event.id)
        async for m in event.client.iter_messages(
            event.chat_id,
            from_user=me.id,
            limit=n + 50,
        ):
            if m.id == event.id:
                continue
            msgs.append(m.id)
            if len(msgs) >= n + 1:
                break
        await event.client.delete_messages(event.chat_id, msgs)
    except MessageDeleteForbiddenError:
        await event.edit(command_card("Del", Texts.Delmsg.NO_PERMS.render(premium=False)), parse_mode="html")
        return
    except FloodWaitError as e:
        await event.edit(
            command_card("Del", await render_for_user(user_id, Texts.Delmsg.FLOOD, seconds=str(e.seconds))),
            parse_mode="html",
        )
        return
    except Exception as e:
        await event.edit(
            command_card("Del", await render_for_user(
                user_id, Texts.Delmsg.ERR,
                etype=type(e).__name__, msg=_h(str(e)),
            )),
            parse_mode="html",
        )
        return
    await event.delete()
