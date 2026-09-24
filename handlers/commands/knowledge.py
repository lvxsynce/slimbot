"""Private-chat UI for selecting chats included in `.ии база`."""

import time

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from utils.storage import get_knowledge_selected_chats, toggle_knowledge_chat
from utils.telethon_manager import telethon_manager
from ._base import command_card


router = Router()
PAGE_SIZE = 8
# Кэш диалогов юзера: unbounded dict — при многих юзерах с сессиями растёт
# вечно. TTL + eviction держат его в границах (10 минут, максимум 64 юзера).
_DIALOGS_CACHE_TTL = 600.0
_DIALOGS_CACHE_MAX_ENTRIES = 64
_dialogs_cache: dict[str, tuple[float, list[dict]]] = {}


def _cache_get(user_id: str) -> list[dict] | None:
    entry = _dialogs_cache.get(user_id)
    if entry is None:
        return None
    ts, dialogs = entry
    if time.time() - ts > _DIALOGS_CACHE_TTL:
        _dialogs_cache.pop(user_id, None)
        return None
    return dialogs


def _cache_set(user_id: str, dialogs: list[dict]) -> None:
    if len(_dialogs_cache) >= _DIALOGS_CACHE_MAX_ENTRIES:
        # Выкидываем самую старую запись (по timestamp), чтобы кэш не рос.
        oldest = min(_dialogs_cache, key=lambda k: _dialogs_cache[k][0])
        _dialogs_cache.pop(oldest, None)
    _dialogs_cache[user_id] = (time.time(), dialogs)


def _is_menu(text: str | None) -> bool:
    parts = (text or "").strip().lower().split()
    return len(parts) == 2 and parts[0] in {".ии", ".ai", ".ии?"} and parts[1] in {"база", "base"}


async def _dialogs(user_id: str) -> list[dict]:
    client = telethon_manager.get_client(user_id)
    if not client:
        return []
    me = await client.get_me()
    own_id = int(me.id)
    result = []
    async for dialog in client.iter_dialogs(folder=None, ignore_migrated=False):
        if int(dialog.id) == own_id:
            continue
        entity = dialog.entity
        title = (
            getattr(entity, "title", None)
            or getattr(entity, "first_name", None)
            or getattr(entity, "username", None)
            or str(dialog.id)
        )
        if getattr(entity, "bot", False):
            kind = "bot"
        elif getattr(entity, "broadcast", False):
            kind = "channel"
        elif getattr(entity, "megagroup", False) or getattr(entity, "participants_count", None) is not None:
            kind = "group"
        else:
            kind = "user"
        result.append({"id": int(dialog.id), "title": str(title), "kind": kind})
    _cache_set(user_id, result)
    return result


async def _render(user_id: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    dialogs = _cache_get(user_id)
    if dialogs is None:
        dialogs = await _dialogs(user_id)
    selected = get_knowledge_selected_chats(user_id)
    total_pages = max(1, (len(dialogs) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    current = dialogs[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = []
    for dialog in current:
        chat_id = dialog["id"]
        prefix = "✓" if chat_id in selected else "○"
        title = dialog["title"].replace("\n", " ")[:42]
        rows.append([InlineKeyboardButton(
            text=f"{prefix} {dialog['kind']} {title}", callback_data=f"kb:t:{page}:{chat_id}"
        )])
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"kb:p:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="kb:noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"kb:p:{page + 1}"))
    rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="Обновить", callback_data=f"kb:r:{page}"),
        InlineKeyboardButton(text="Статус", callback_data="kb:s"),
    ])
    rows.append([
        InlineKeyboardButton(text="Запустить сбор", callback_data="kb:start"),
        InlineKeyboardButton(text="Пауза", callback_data="kb:stop"),
    ])
    text = command_card(
        "AI database",
        "<b>База знаний</b>\n"
        f"Выбрано чатов: <code>{len(selected)}</code> из <code>{len(dialogs)}</code>\n"
        "Отметь только те переписки, которые должны участвовать в поиске и сборе.",
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.chat.type == "private", lambda m: _is_menu(m.text))
async def knowledge_menu(message: types.Message):
    uid = str(message.from_user.id) if message.from_user else ""
    if not telethon_manager.get_client(uid):
        await message.reply(command_card("AI database", "[?] Для базы нужна активная Telethon-сессия."), parse_mode="html")
        return
    try:
        text, keyboard = await _render(uid, 0)
    except Exception:
        await message.reply(command_card("AI database", "[x] Не удалось получить список чатов. Попробуй ещё раз."), parse_mode="html")
        return
    await message.reply(text, reply_markup=keyboard, parse_mode="html")


@router.callback_query(F.data.startswith("kb:"))
async def knowledge_callback(callback: types.CallbackQuery):
    uid = str(callback.from_user.id) if callback.from_user else ""
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    data = callback.data or ""
    try:
        parts = data.split(":")
        action = parts[1]
        if action == "noop":
            await callback.answer()
            return
        if action == "t" and len(parts) == 4:
            page, chat_id = int(parts[2]), int(parts[3])
            dialogs = _cache_get(uid)
            if dialogs is None:
                dialogs = await _dialogs(uid)
            if chat_id not in {item["id"] for item in dialogs}:
                await callback.answer("Чат больше недоступен", show_alert=True)
                return
            enabled = toggle_knowledge_chat(uid, chat_id)
            await callback.answer("Добавлен" if enabled else "Убран")
            text, keyboard = await _render(uid, page)
        elif action == "p" and len(parts) == 3:
            text, keyboard = await _render(uid, int(parts[2]))
            await callback.answer()
        elif action == "r" and len(parts) == 3:
            _dialogs_cache.pop(uid, None)
            text, keyboard = await _render(uid, int(parts[2]))
            await callback.answer("Список обновлён")
        elif action == "start":
            from utils.knowledge_collector import knowledge_collector
            client = telethon_manager.get_client(uid)
            if not client:
                await callback.answer("Telethon-сессия не активна", show_alert=True)
                return
            started, note = await knowledge_collector.start(uid, client)
            await callback.answer("Сбор запущен" if started else note.replace("<code>", "").replace("</code>", ""), show_alert=not started)
            text, keyboard = await _render(uid, 0)
        elif action == "stop":
            stopped = await telethon_manager._knowledge_collector.stop(uid)
            await callback.answer("Сбор приостановлен" if stopped else "Активного сбора нет")
            text, keyboard = await _render(uid, 0)
        elif action == "s":
            from handlers.commands.ai import _do_knowledge
            await callback.answer()
            await callback.message.answer(
                command_card("AI database", await _do_knowledge(uid, "статус", telethon_manager.get_client(uid))),
                parse_mode="html",
            )
            return
        else:
            await callback.answer()
            return
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="html")
        except TelegramBadRequest:
            pass
    except Exception:
        # Широкий catch: collector/start могут упасть на FloodWait или
        # сетевой ошибке — юзер должен увидеть ответ, а не молчаливый сбой.
        try:
            await callback.answer("Ошибка: попробуй ещё раз", show_alert=True)
        except Exception:
            pass
