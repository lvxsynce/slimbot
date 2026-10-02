"""Команда .влс / .лс / .dm — отправить текст автору replied в ЛС. Telethon-only."""
from utils.cmds import DM_CMDS

from utils.texts import Texts, render_for_user
from utils.telethon_manager import telethon_reply_to
from handlers.commands._base import command_card





async def handle(user_id: str, event) -> None:
    """Telethon-вызов из telethon_manager._handle_outgoing."""
    text = (event.raw_text or "").strip()
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else "."

    reply = await event.get_reply_message()
    if not reply:
        await event.edit(command_card("DM", Texts.Dm.NEED_REPLY.render(premium=False)), parse_mode="html")
        return

    sender = await reply.get_sender()
    if not sender:
        await event.edit(command_card("DM", Texts.Dm.NEED_REPLY.render(premium=False)), parse_mode="html")
        return

    try:
        await event.client.send_message(sender, body)
        try:
            await event.delete()
        except Exception:
            pass
    except Exception as e:
        from html import escape as _h
        await event.edit(
            command_card("DM", f"[x] {type(e).__name__}: {_h(str(e))}"),
            parse_mode="html",
        )
