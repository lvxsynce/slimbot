"""@bot статус — карточка состояния бота.

Только для пользователей с Telethon-сессией. Остальным
выдаём карточку-приглашение + switch_pm-кнопку.

Этот модуль экспортирует async-функцию `handle`, вызываемую из dispatch_inline.
"""

import time

from aiogram import Bot
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from utils.bot_info import bot_username_at
from utils.storage import user_sessions
from utils.telethon_manager import telethon_manager

from . import connect_button, uptime_seconds


STATUS_KEYWORDS = ("статус", "status")
CACHE_TIME = 15


def is_status_query(q: str) -> bool:
    """True, если q соответствует команде «статус»."""
    return q.strip().lower() in STATUS_KEYWORDS


def _fmt_uptime(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{int(seconds)} сек"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}м {s}с"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}ч {m}м"
    d, h = divmod(h, 24)
    return f"{d}д {h}ч"


async def _bot_ping(bot: Bot) -> int | None:
    """Round-trip до Telegram через getMe (aiogram не кэширует этот метод)."""
    t0 = time.monotonic()
    try:
        await bot.get_me()
    except Exception:
        return None
    return int((time.monotonic() - t0) * 1000)


async def _build_status_text(bot: Bot) -> str:
    ping = await _bot_ping(bot)
    up = _fmt_uptime(uptime_seconds())
    clients = getattr(telethon_manager, "_clients", None)
    active_clients = len(clients) if clients else 0
    sessions_total = len(user_sessions)

    ping_line = (
        f"Пинг до Telegram (getMe): <code>{ping}ms</code>"
        if ping is not None
        else "Пинг: <code>—</code>"
    )

    lines = [
        "<b>📊 Статус бота</b>",
        f"Аптайм: <code>{up}</code>",
        ping_line,
        f"Активных Telethon-клиентов: <code>{active_clients}</code>",
        f"Telethon-сессий в базе: <code>{sessions_total}</code>",
    ]
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


async def handle(inline: InlineQuery, bot: Bot) -> None:
    """Обрабатывает inline-запрос @bot статус."""
    from utils.storage import session_exists
    uid = str(inline.from_user.id)
    bot_at = bot_username_at()

    if not session_exists(uid):
        text = (
            "📊 <b>Статус доступен с Telethon-сессией.</b>\n\n"
            f"Открой личку с {bot_at} и нажми <b>/start → [+] Включить</b>. "
            "После этого inline <code>"
            f"{bot_at} статус</code> "
            "покажет аптайм, пинг и нагрузку."
        )
        await inline.answer(
            results=[
                InlineQueryResultArticle(
                    id="status_locked",
                    title="Нужна Telethon-сессия",
                    description=f"Включи сессию в личке с {bot_at}.",
                    input_message_content=InputTextMessageContent(
                        message_text=text,
                        parse_mode="HTML",
                    ),
                ),
            ],
            cache_time=10,
            is_personal=True,
            button=connect_button(),
        )
        return

    text = await _build_status_text(bot)
    await inline.answer(
        results=[
            InlineQueryResultArticle(
                id="status_main",
                title="📊 Статус бота (live)",
                description="Аптайм · пинг · нагрузка",
                input_message_content=InputTextMessageContent(
                    message_text=text,
                    parse_mode="HTML",
                ),
            ),
        ],
        cache_time=CACHE_TIME,
        is_personal=True,
    )
