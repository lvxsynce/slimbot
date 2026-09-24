"""Команда .ня — включить/выключить catgirl-rewrite исходящих сообщений юзера
в ТЕКУЩЕМ чате. Telethon-only фича.

aiogram НЕ МОЖЕТ перехватить исходящие сообщения юзера (Bot API не даёт
прав на чтение/редактирование чужих сообщений в личке). Поэтому фича
работает ТОЛЬКО через Telethon-сессию юзера и напрямую вызывается из
utils/telethon_manager.py::_handle_outgoing.

Подкоманды:
- ``.ня`` (без аргументов)  → toggle ON в текущем чате.
- ``.ня выкл`` / ``.ня стоп`` / ``.ня off`` / ``.ня stop`` → OFF.
- ``.ня список`` / ``.ня list`` → список chat_id с включённым режимом.

Не работает в личке с ботом (``event.is_private and chat.bot``) — там
просто нечего редактировать вне исходящих сообщений юзера, и технически
невозможно перехватить их через Bot API.

Перехват ВСЕХ сообщений юзера в чатах с включённым режимом происходит
в TelethonManager._handle_outgoing ДО elif-chain для команд, и в TelethonManager._apply_nya
(отдельный async-task) сами edit'ы через ``asyncio.wait_for(event.edit(...))``.
"""

import asyncio
from html import escape as _h

from utils.texts import Texts
from utils.storage import (
    is_nya_chat,
    get_nya_chats,
    toggle_nya_chat,
)
from utils.telethon_manager import telethon_manager
from handlers.commands._base import command_card


NYA_CMDS = (".ня",)
NYA_OFF_ALIASES = ("стоп", "stop", "выкл", "off", "откл")
NYA_LIST_ALIASES = ("список", "list", "все")


def _check(t: str | None) -> bool:
    """Возвращает True, если сообщение начинается с `.ня` (с пробелом или без)."""
    if not t:
        return False
    head = t.strip().lower().split()[0]
    return head in NYA_CMDS


async def handle(user_id: str, event) -> None:
    """Telethon-вызов из telethon_manager._handle_outgoing.

    Args:
        user_id: ID юзера (str).
        event:   Telethon ``NewMessage.Event`` (.raw_text с командой).
    """
    # Личка с ботом — нечего редактировать вне исходящих сообщений юзера
    # через MTProto-сессию. Telethon хук ``events.NewMessage(outgoing=True)``
    # тут вообще не сработает (bot chat), но на всякий случай guard.
    if event.is_private:
        chat = await event.get_chat()
        if getattr(chat, "bot", False):
            await event.edit(command_card("Nya", Texts.Nya.PRIVATE.render(premium=False)), parse_mode="html")
            return
        # Личка с человеком (не ботом) — тоже не имеет смысла: catgirl-rewrite
        # работает только с Telethon-сессией, и в личке это сбивает ритм
        # переписки. Но технически возможно; пока разрешаем (mode sends in dms).

    text = (event.raw_text or "").strip()
    # Парсим подкоманду ПОСЛЕ `.ня `: ``.ня``, ``.ня стоп``, ``.ня список``, …
    parts = text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    chat_id = event.chat_id

    if arg in NYA_LIST_ALIASES:
        await _handle_list(user_id, event)
        return

    if arg in NYA_OFF_ALIASES:
        # Capture PRE-toggle state — toggle_nya_chat(state=False) always
        # returns False, и no-op от успешного удаления не различить
        # по одному только return value. Поэтому сначала запоминаем,
        # был ли чат в списке, потом дёргаем toggle, потом диспатчим
        # по этому snapshot'у.
        was_on = is_nya_chat(user_id, chat_id)
        toggle_nya_chat(user_id, chat_id, state=False)
        text_obj = Texts.Nya.OFF_OK if was_on else Texts.Nya.OFF_NOT
        await event.edit(command_card("Nya", text_obj.render(premium=False)), parse_mode="html")
        return

    # Дефолт: toggle ON.
    if is_nya_chat(user_id, chat_id):
        await event.edit(
            command_card("Nya", Texts.Nya.ON_ALREADY.render(premium=False)),
            parse_mode="html",
        )
        return
    toggle_nya_chat(user_id, chat_id, state=True)
    await event.edit(
        command_card("Nya", Texts.Nya.ON_OK.render(premium=False)),
        parse_mode="html",
    )


async def _handle_list(user_id: str, event) -> None:
    """``.ня список`` — список чатов с включённым режимом.

    Резолвим chat_id → title через Telethon-клиент юзера. HTML-escape
    для защиты от инъекций через имя чата — по аналогии с
    ``_handle_tr_list`` в telethon_manager.py.
    """
    from utils.telethon_manager import telethon_manager

    chat_ids = get_nya_chats(user_id)
    if not chat_ids:
        await event.edit(
            command_card("Nya", Texts.Nya.LIST_EMPTY.render(premium=False)),
            parse_mode="html",
        )
        return

    client = telethon_manager.get_client(str(user_id))
    lines: list[str] = []
    for cid in chat_ids:
        title = str(cid)
        if client:
            try:
                ent = await asyncio.wait_for(client.get_entity(cid), timeout=5)
                title = (
                    getattr(ent, "title", None)
                    or getattr(ent, "first_name", None)
                    or str(cid)
                )
            except Exception:
                title = str(cid)
        lines.append(f"• <b>{_h(title)}</b> (<code>{cid}</code>)")

    await event.edit(
        command_card(
            "Nya",
            Texts.Nya.LIST_HEADER.render(premium=False) + "\n" + "\n".join(lines),
        ),
        parse_mode="html",
    )
