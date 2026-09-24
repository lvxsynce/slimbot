"""Команда .ссылка / .invitelink / .инвайт — получить/создать инвайт-ссылку. Telethon-only.

NB: `ExportChatInviteRequest` и `GetExportedChatInvitesRequest` живут в
`telethon.tl.functions.messages`, а НЕ в `channels` — это частая ошибка,
когда смотрят в Pyrogram/TDLib. Канал и супергруппа в TL-схеме всё равно
адресуются через `InputPeer` (а не `InputChannel`), так что эти функции
работают для всех типов чатов.
"""

from telethon.tl.functions.messages import (
    ExportChatInviteRequest,
    GetExportedChatInvitesRequest,
)
from telethon.errors import ChatAdminRequiredError

from utils.texts import Texts, render_for_user
from handlers.commands._base import command_card

INVITE_CMDS = (".ссылка", ".invitelink", ".инвайт", ".invite")


def _check(t: str | None) -> bool:
    if not t:
        return False
    head = t.strip().lower().split()[0]
    return head in INVITE_CMDS


async def handle(user_id: str, event) -> None:
    from html import escape as _h
    chat = await event.get_chat()
    if not (getattr(chat, "megagroup", False) or getattr(chat, "broadcast", False)):
        # В личке — нет смысла.
        await event.edit(command_card("Invite link", Texts.Invitelink.NEED_ADMIN.render(premium=False)), parse_mode="html")
        return

    await event.edit(
        command_card("Invite link", await render_for_user(user_id, Texts.Invitelink.GETTING)),
        parse_mode="html",
    )
    try:
        # Сначала пробуем получить существующую инвайт-ссылку.
        try:
            res = await event.client(GetExportedChatInvitesRequest(
                peer=event.chat_id,
                admin_id=(await event.client.get_me()).id,
                limit=1,
            ))
            if res.invites:
                link = res.invites[0].link
                await event.edit(f"<b>Slim bot | Invite link</b>\n<blockquote><code>{_h(link)}</code></blockquote>", parse_mode="html")
                return
        except Exception:
            pass
        # Иначе создаём новую.
        try:
            invite = await event.client(ExportChatInviteRequest(peer=event.chat_id))
            link = invite.link
            await event.edit(f"<b>Slim bot | Invite link</b>\n<blockquote><code>{_h(link)}</code></blockquote>", parse_mode="html")
            return
        except Exception:
            pass
        await event.edit(
            command_card("Invite link", await render_for_user(
                user_id, Texts.Invitelink.ERR,
                etype="Error", msg="не удалось получить или создать",
            )),
            parse_mode="html",
        )
    except ChatAdminRequiredError:
        await event.edit(command_card("Invite link", Texts.Invitelink.NEED_ADMIN.render(premium=False)), parse_mode="html")
    except Exception as e:
        await event.edit(
            command_card("Invite link", await render_for_user(
                user_id, Texts.Invitelink.ERR,
                etype=type(e).__name__, msg=_h(str(e)),
            )),
            parse_mode="html",
        )
