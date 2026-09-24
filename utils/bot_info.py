"""Утилита для динамического получения username бота.

Чтобы избежать хардкода '@slimbot' в пользовательских текстах, все
упоминания бота в handler'ах и inline-карточках должны идти через эти
хелперы. После bot.py::on_startup() в cfg.BOT_USERNAME подставляется
реальный me.username из Telegram (например, 'Slimsec_robot').

Использовать только в runtime-контексте (на каждый запрос), не на
уровне module-scope — username становится известным ПОСЛЕ импорта модулей.
"""
import config as cfg
from config import DEFAULT_BOT_USERNAME


DEFAULT_USERNAME = DEFAULT_BOT_USERNAME


def bot_username() -> str:
    """Возвращает username бота БЕЗ '@'.

    Fallback 'slimbot' если on_startup ещё не отработал (например,
    при первом import или в unit-тестах).
    """
    return cfg.BOT_USERNAME or DEFAULT_USERNAME


def bot_username_at() -> str:
    """Возвращает @username бота (например, '@Slimsec_robot')."""
    u = bot_username()
    return f"@{u}" if u else f"@{DEFAULT_USERNAME}"


def bot_mention_html() -> str:
    """HTML-ссылка на бота (`<a href="https://t.me/...">@Slimsec_robot</a>`)."""
    u = bot_username()
    if not u:
        return f"@{DEFAULT_USERNAME}"
    return f'<a href="https://t.me/{u}">@{u}</a>'
