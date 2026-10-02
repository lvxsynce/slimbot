"""Команда .del / .удалить — удалить N своих сообщений. Telethon-only."""

import logging

from telethon.errors import FloodWaitError, MessageDeleteForbiddenError

from utils.texts import Texts, render_for_user
from handlers.commands._base import command_card

logger = logging.getLogger(__name__)


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

    # Сначала собираем id-шники. Команда сама (`event.id`) ВСЕГДА в списке,
    # остальные N добираются через iter_messages от автора команды.
    #
    # Падение delete_messages НЕ должно приводить к `event.edit(...)`:
    # сообщение команды может уже быть удалено, и edit бросит
    # MessageIdInvalidError. Пользователю в этом случае просто нечего
    # показать — результат уже применён (или не применён) в чате.
    try:
        me = await event.client.get_me()
        msgs = [event.id]
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
        await _safe_edit(
            event, command_card("Del", Texts.Delmsg.NO_PERMS.render(premium=False))
        )
        return
    except FloodWaitError as e:
        await _safe_edit(
            event,
            command_card(
                "Del",
                await render_for_user(user_id, Texts.Delmsg.FLOOD, seconds=str(e.seconds)),
            ),
        )
        return
    except Exception as e:
        await _safe_edit(
            event,
            command_card(
                "Del",
                await render_for_user(
                    user_id, Texts.Delmsg.ERR,
                    etype=type(e).__name__, msg=_h(str(e)),
                ),
            ),
        )
        return

    # Команда уже удалена вместе со списком выше. Раньше здесь стоял
    # безусловный `await event.delete()` — второе удаление того же id
    # кидало MessageIdInvalidError наружу, и outgoing_handler логировал
    # необработанное исключение на КАЖДЫЙ `.del`.
    return


async def _safe_edit(event, text: str) -> None:
    """edit, переживающий уже удалённое сообщение команды.

    Если само сообщение уже удалено (или текст не изменился), edit бросит
    MessageIdInvalidError / MessageNotModifiedError. Это не ошибка команды —
    молча проглатываем.
    """
    try:
        await event.edit(text, parse_mode="html")
    except Exception:
        logger.debug("delmsg: edit after delete failed", exc_info=True)
