"""Помощь для inline-режима.

Этот модуль экспортирует async-функцию `handle`, которая вызывается
напрямую из handlers/inline/__init__.py::dispatch_inline (а не через
@router.inline_query фильтр), чтобы избежать двойной диспетчеризации
в aiogram 3.x.

Username бота подставляется динамически — функциями ниже тексты
собираются при каждом запросе (а не на module-load), потому что
cfg.BOT_USERNAME доступен только после bot.py::on_startup().
"""

from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from utils.bot_info import bot_username_at, bot_mention_html
from utils.storage import session_exists

from . import connect_button


HELP_KEYWORDS = ("помощь", "help", "справка", "h", "?")


def is_help_query(q: str) -> bool:
    """True, если q соответствует команде «помощь»."""
    s = q.strip().lower()
    return not s or s in HELP_KEYWORDS


def _overview_text() -> str:
    """Полная справка по inline — собирается на каждый запрос с актуальным username бота."""
    bot = bot_username_at()
    link = bot_mention_html()
    return (
        f"<b>Slim bot | Inline help</b>\n\n"
        f"Доступны в любом чате: набери <code>{bot}</code> → Telegram покажет подсказки.\n\n"
        f"• <code>{bot}</code> — это сообщение\n"
        f"• <code>{bot} статус</code> — карточка состояния бота\n"
        f"• <code>{bot} @username</code> — карточка профиля (нужна Telethon-сессия)\n\n"
        "<b>🔧 Dot-команды</b>\n"
         "Многие команды ещё лучше работают через <code>.команда</code> "
         "внутри чатов: <code>.ping</code>, <code>.time</code>, <code>.id</code>, "
         "<code>.net</code>, "
        "<code>.tr</code>, <code>.calc</code>, <code>.hash</code>, <code>.watch</code>.\n\n"
        f"<i>Открой личку с {link} → <b>/start</b>, чтобы увидеть весь список.</i>"
    )


def _unauth_text() -> str:
    bot = bot_username_at()
    link = bot_mention_html()
    return (
        f"<b>🙋 {bot} inline</b>\n\n"
        "Чтобы использовать inline-команды, нужно подключить бота:\n"
        f"1. Открой {link} в личке\n"
        "2. Нажми <b>/start</b>\n"
        "3. Следуй инструкциям\n\n"
        "<i>Подключение занимает минуту.</i>"
    )


def _make_article(
    title: str,
    description: str,
    text: str,
    id_: str,
) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=id_,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=text,
            parse_mode="HTML",
        ),
    )


async def handle(inline: InlineQuery) -> None:
    """Обрабатывает inline-запрос @bot | @bot помощь | @bot help."""
    uid = str(inline.from_user.id)
    # Авторизация в inline = Telethon-сессия (отдельного Business-коннекта больше нет).
    authorized = session_exists(uid)
    bot = bot_username_at()

    if not authorized:
        await inline.answer(
            results=[
                _make_article(
                    title=f"Подключи {bot}",
                    description="Inline-режим доступен после подключения.",
                    text=_unauth_text(),
                    id_="help_unauthorized",
                ),
            ],
            cache_time=30,
            is_personal=True,
            button=connect_button(),
        )
        return

    await inline.answer(
        results=[
            _make_article(
                title=f"{bot} — справка",
                description="Доступно из любого чата после подключения бота.",
                text=_overview_text(),
                id_="help_main",
            ),
        ],
        cache_time=300,
        is_personal=True,
    )
