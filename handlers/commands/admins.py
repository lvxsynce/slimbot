"""Команда .admins / .админы — список администраторов чата. Telethon-only."""

from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import (
    ChannelParticipantsAdmins,
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
)

from utils.texts import Texts, render_for_user
from handlers.commands._base import command_card

ADMINS_CMDS = (".admins", ".админы")


def _check(t: str | None) -> bool:
    if not t:
        return False
    head = t.strip().lower().split()[0]
    return head in ADMINS_CMDS


async def handle(user_id: str, event) -> None:
    from html import escape as _h
    chat = await event.get_chat()
    if not (getattr(chat, "megagroup", False) or getattr(chat, "broadcast", False)):
        await event.edit(command_card("Admins", Texts.Admins.ONLY_GROUPS.render(premium=False)), parse_mode="html")
        return
    try:
        result = await event.client(GetParticipantsRequest(
            channel=event.chat_id,
            filter=ChannelParticipantsAdmins(),
            offset=0,
            limit=200,
            hash=0,
        ))
        if not result.participants:
            await event.edit(command_card("Admins", Texts.Admins.EMPTY.render(premium=False)), parse_mode="html")
            return
        admins = []
        owner = None
        for p in result.participants:
            user_id_p = getattr(p, "user_id", None)
            if not user_id_p:
                continue
            ent = await event.client.get_entity(user_id_p)
            if isinstance(p, ChannelParticipantCreator):
                owner = ent
            else:
                admins.append(ent)
        title = await render_for_user(user_id, Texts.Admins.TITLE, n=str(len(result.participants)))
        lines = [title]
        if owner:
            uname = getattr(owner, "username", None)
            if uname:
                lines.append(f"👑 <a href=\"https://t.me/{_h(uname)}\">{_h(uname)}</a> <i>(owner)</i>")
            else:
                name = (getattr(owner, "first_name", "") or "").strip() or str(owner.id)
                lines.append(f"👑 <a href=\"tg://user?id={owner.id}\">{_h(name)}</a> <i>(owner)</i>")
        for a in admins[:30]:
            uname = getattr(a, "username", None)
            if uname:
                lines.append(f"• <a href=\"https://t.me/{_h(uname)}\">{_h(uname)}</a>")
            else:
                name = (getattr(a, "first_name", "") or "").strip() or str(a.id)
                lines.append(f"• <a href=\"tg://user?id={a.id}\">{_h(name)}</a>")
        await event.edit(command_card("Admins", "\n".join(lines)), parse_mode="html")
    except Exception as e:
        await event.edit(
            command_card("Admins", await render_for_user(
                user_id, Texts.Admins.ERR,
                etype=type(e).__name__, msg=_h(str(e)),
            )),
            parse_mode="html",
        )
