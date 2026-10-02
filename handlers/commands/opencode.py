"""Команда .опенкод — статистика токенов OpenCode за день и неделю."""

import aiohttp
import logging
import os
from datetime import datetime, timezone as tz

from aiogram import Router, types

from utils import rate_limit_gate as gate
from utils.escape import esc
from ._base import command_card, thread_kwargs

logger = logging.getLogger(__name__)
router = Router()

OPENCODE_API_URL = os.getenv("OPENCODE_API_URL", "https://go.gogetfree.ru/")
OPENCODE_API_PASS = os.getenv("OPENCODE_API_PASS", "")


def _num(v) -> int:
    """Безопасно приводит значение из внешнего JSON к int (мусор → 0)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


async def fetch_opencode_stats():
    """Получить статистику OpenCode."""
    if not OPENCODE_API_PASS:
        logger.warning("OpenCode API pass is not configured (OPENCODE_API_PASS).")
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENCODE_API_URL,
                json={"pass": OPENCODE_API_PASS},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.warning(f"OpenCode API returned {resp.status}")
                    return None
    except Exception as e:
        logger.error(f"Failed to fetch OpenCode stats: {e}")
        return None


def format_opencode_stats(data: dict) -> str:
    """Форматировать статистику OpenCode в HTML."""
    if not data or "data" not in data or "opencode_tokens" not in data["data"]:
        return command_card("OpenCode", "[x] Не удалось получить статистику OpenCode")

    oc = data["data"]["opencode_tokens"]
    if not oc.get("available"):
        return command_card("OpenCode", "[x] Статистика OpenCode недоступна")

    today = oc.get("today", {})
    week = oc.get("week", {})

    lines = [
        "<blockquote>",
        "<b>📊 OpenCode — Статистика токенов</b>",
        "",
    ]

    # День
    if today:
        lines.append("<b>🕐 Сегодня</b>")
        lines.append(f"• Сообщений: <code>{_num(today.get('messages')):,}</code>")
        lines.append(
            f"• Ввод: <code>{_num(today.get('input')):,}</code> токенов"
        )
        lines.append(
            f"• Выход: <code>{_num(today.get('output')):,}</code> токенов"
        )
        lines.append(
            f"• Кэш (чтение): <code>{_num(today.get('cache_read')):,}</code> токенов"
        )
        lines.append(
            f"• Всего: <code>{_num(today.get('total')):,}</code> токенов"
        )

        by_model = today.get("by_model", {})
        if by_model:
            lines.append("<i>По моделям:</i>")
            for model, stats in sorted(by_model.items()):
                lines.append(
                    f"  • {esc(model)}: {_num(stats.get('input')):,} ввод, {_num(stats.get('output')):,} выход"
                )
        lines.append("")

    # Неделя
    if week:
        lines.append("<b>📅 За неделю</b>")
        lines.append(f"• Сообщений: <code>{_num(week.get('messages')):,}</code>")
        lines.append(
            f"• Ввод: <code>{_num(week.get('input')):,}</code> токенов"
        )
        lines.append(
            f"• Выход: <code>{_num(week.get('output')):,}</code> токенов"
        )
        lines.append(
            f"• Кэш (чтение): <code>{_num(week.get('cache_read')):,}</code> токенов"
        )
        lines.append(
            f"• Всего: <code>{_num(week.get('total')):,}</code> токенов"
        )

        by_model = week.get("by_model", {})
        if by_model:
            lines.append("<i>ТОП провайдеров:</i>")
            sorted_models = sorted(
                by_model.items(),
                key=lambda x: _num(x[1].get("total")),
                reverse=True,
            )[:3]
            for model, stats in sorted_models:
                lines.append(
                    f"  • {esc(model)}: {_num(stats.get('total')):,} токенов"
                )
        lines.append("")

    lines.append(
        f"<i>Обновлено: {datetime.now(tz.utc).strftime('%H:%M:%S UTC')}</i>"
    )
    lines.append("</blockquote>")

    return command_card("OpenCode", "\n".join(lines))


def _check(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    return t in (".опенкод", ".opencode")


async def _limited(uid: str) -> bool:
    """Лимит на `.опенкод` через единый гейт.

    Внешний HTTP-сервис, доступный любому, кто нашёл бота; раньше лимита
    не было ни здесь, ни в Telethon-пути.
    """
    return gate.check(".опенкод", uid)


@router.message(lambda msg: _check(msg.text))
async def cmd_opencode_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    if not await _limited(uid):
        await message.reply(
            command_card("OpenCode", "[x] Слишком много запросов. Подожди немного."),
            parse_mode="html", **thread_kwargs(message),
        )
        return
    stats = await fetch_opencode_stats()
    text = (
        format_opencode_stats(stats)
        if stats
        else command_card("OpenCode", "[x] Ошибка подключения")
    )
    await message.reply(text, parse_mode="html", **thread_kwargs(message))


async def handle(user_id: str, event):
    """Telethon-вызов из telethon_manager._handle_outgoing."""
    # Лимит уже проверен в _handle_outgoing (`.opencode` в списке network).
    stats = await fetch_opencode_stats()
    text = (
        format_opencode_stats(stats)
        if stats
        else command_card("OpenCode", "[x] Ошибка подключения")
    )
    await event.edit(text, parse_mode="html")
