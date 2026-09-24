"""Команда .pin / .закрепить / .unpin / .открепить. Telethon-only.

Использует `messages.UpdatePinnedMessageRequest` напрямую (он работает для
любых peer — User, Chat, Channel). В Telethon НЕТ отдельного
`UpdateChannelPinnedMessageRequest` — это частая ошибка, когда смотрят в
исходники Pyrogram / TDLib. Поэтому в коде только ОДИН импорт.
"""

from telethon.tl.functions.messages import UpdatePinnedMessageRequest

from utils.texts import Texts, render_for_user
from handlers.commands._base import command_card

PIN_CMDS = (".pin", ".закрепить", ".закреп")
UNPIN_CMDS = (".unpin", ".открепить", ".раскрепить", ".откреп")


def _check(t: str | None) -> bool:
    if not t:
        return False
    head = t.strip().lower().split()[0]
    return head in PIN_CMDS or head in UNPIN_CMDS


async def handle(user_id: str, event) -> None:
    from html import escape as _h
    text = (event.raw_text or "").strip()
    parts = text.split()
    head = parts[0].lower() if parts else ""
    args = " ".join(parts[1:]) if len(parts) > 1 else ""

    if head in PIN_CMDS:
        await _do_pin(user_id, event, args)
    else:
        await _do_unpin(user_id, event, args)


async def _do_pin(user_id: str, event, args: str):
    from html import escape as _h
    silent = True
    if args:
        first = args.split()[0].lower()
        if first in ("loud", "громко", "no", "ns"):
            silent = False
        elif first in ("silent", "тихо", "n"):
            silent = True

    reply = await event.get_reply_message()
    target_id = reply.id if reply else event.id

    await event.edit(
        command_card("Pin", await render_for_user(user_id, Texts.Pin.PINNING)),
        parse_mode="html",
    )
    try:
        await event.client(UpdatePinnedMessageRequest(
            peer=event.chat_id,
            id=target_id,
            silent=silent,
        ))
        try:
            await event.delete()
        except Exception:
            pass
    except Exception as e:
        etype = type(e).__name__
        msg = _h(str(e))
        if "permission" in msg.lower() or "admin" in msg.lower():
            await event.edit(command_card("Pin", Texts.Pin.NEED_ADMIN.render(premium=False)), parse_mode="html")
        elif "disabled" in msg.lower():
            await event.edit(command_card("Pin", Texts.Pin.DISABLED.render(premium=False)), parse_mode="html")
        else:
            await event.edit(
                command_card("Pin", await render_for_user(user_id, Texts.Pin.ERR, etype=etype, msg=msg)),
                parse_mode="html",
            )


async def _do_unpin(user_id: str, event, args: str):
    from html import escape as _h
    if args.lower() in ("all", "все"):
        await event.edit(
            command_card("Unpin", await render_for_user(user_id, Texts.Pin.UNPINNING_ALL)),
            parse_mode="html",
        )
        try:
            await event.client(UpdatePinnedMessageRequest(
                peer=event.chat_id,
                id=0,
                silent=True,
            ))
            await event.edit(
                command_card("Unpin", await render_for_user(user_id, Texts.Pin.DONE_ALL_UNPIN)),
                parse_mode="html",
            )
        except Exception as e:
            etype = type(e).__name__
            msg = _h(str(e))
            if "permission" in msg.lower() or "admin" in msg.lower():
                await event.edit(command_card("Unpin", Texts.Pin.NEED_ADMIN_UNPIN.render(premium=False)), parse_mode="html")
            else:
                await event.edit(
                    command_card("Unpin", await render_for_user(user_id, Texts.Pin.ERR, etype=etype, msg=msg)),
                    parse_mode="html",
                )
        return

    reply = await event.get_reply_message()
    if reply:
        target_id = reply.id
        label = "сообщение"
    else:
        # Без reply — снимаем последнее закреплённое.
        async for m in event.client.iter_messages(event.chat_id, pinned=True, limit=1):
            target_id = m.id
            label = "последнее закреплённое"
            break
        else:
            target_id = 0
            label = "закрепление"
    await event.edit(
        command_card("Unpin", await render_for_user(user_id, Texts.Pin.UNPINNING_ONE, label=label)),
        parse_mode="html",
    )
    try:
        await event.client(UpdatePinnedMessageRequest(
            peer=event.chat_id,
            id=target_id,
            silent=True,
        ))
        await event.edit(
            command_card("Unpin", await render_for_user(user_id, Texts.Pin.DONE_ONE_UNPIN, label=label)),
            parse_mode="html",
        )
    except Exception as e:
        etype = type(e).__name__
        msg = _h(str(e))
        if "permission" in msg.lower() or "admin" in msg.lower():
            await event.edit(command_card("Unpin", Texts.Pin.NEED_ADMIN_UNPIN.render(premium=False)), parse_mode="html")
        else:
            await event.edit(
                command_card("Unpin", await render_for_user(user_id, Texts.Pin.ERR, etype=etype, msg=msg)),
                parse_mode="html",
            )
