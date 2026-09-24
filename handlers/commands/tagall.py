"""Команда .tagall / .все — тегнуть всех в группе. Telethon-only."""

from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
from telethon.errors import ChatAdminRequiredError

from utils.texts import Texts, render_for_user
from utils.telethon_manager import telethon_reply_to
from handlers.commands._base import command_card

TAGALL_CMDS = (".tagall", ".тегвсех", ".все")


def _check(t: str | None) -> bool:
    if not t:
        return False
    head = t.strip().lower().split()[0]
    return head in TAGALL_CMDS


async def handle(user_id: str, event) -> None:
    from html import escape as _h
    chat = await event.get_chat()
    if not getattr(chat, "megagroup", False) and not getattr(chat, "broadcast", False):
        await event.edit(command_card("Tagall", Texts.Tagall.ONLY_GROUPS.render(premium=False)), parse_mode="html")
        return
    text = (event.raw_text or "").strip()
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    try:
        participants = []
        async for p in event.client.iter_participants(event.chat_id, limit=100):
            if getattr(p, "bot", False) or getattr(p, "deleted", False):
                continue
            participants.append(p)
        if not participants:
            await event.edit(command_card("Tagall", Texts.Tagall.NO_PARTICIPANTS.render(premium=False)), parse_mode="html")
            return
        # До 50 mentions — hard-cap Telegram на одно сообщение.
        targets = participants[:50]
        if not targets:
            await event.edit(command_card("Tagall", Texts.Tagall.NO_USERS.render(premium=False)), parse_mode="html")
            return

        # Чанки по 5 для читаемости.
        lines = []
        chunk_size = 5
        for i in range(0, len(targets), chunk_size):
            chunk = targets[i:i + chunk_size]
            lines.append(" ".join(_mention(p) for p in chunk))
        msg_body = "\n".join(lines)
        if body:
            msg_body = f"<b>{_h(body)}</b>\n\n" + msg_body
        await event.client.send_message(
            event.chat_id,
            msg_body,
            reply_to=telethon_reply_to(event),
            parse_mode="html",
        )
        try:
            await event.delete()
        except Exception:
            pass
    except ChatAdminRequiredError:
        await event.edit(command_card("Tagall", Texts.Tagall.NEED_ADMIN.render(premium=False)), parse_mode="html")
    except Exception as e:
        await event.edit(
            command_card("Tagall", await render_for_user(
                user_id, Texts.Tagall.ERR,
                etype=type(e).__name__, msg=_h(str(e)),
            )),
            parse_mode="html",
        )


def _mention(p) -> str:
    from html import escape as _h
    name = (getattr(p, "first_name", "") or "").strip()
    if not name and getattr(p, "username", None):
        name = f"@{p.username}"
    if not name:
        name = str(p.id)
    return f'<a href="tg://user?id={p.id}">{_h(name)}</a>'
