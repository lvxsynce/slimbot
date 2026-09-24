"""Inline-режим @slimbot: точка сборки и единый dispatcher.

Доступные команды (диспатчер определяет по `InlineQuery.query`):
- пустой / помощь / help         → handlers/inline/help.py:handle
- статус / status                → handlers/inline/status.py:handle
- @username                      → handlers/inline/profile.py:handle

Архитектурный note: в aiogram 3.x dispatcher прогоняет ВСЕ observers,
чьи фильтры прошли — для inline_query это привело бы к двойному
inline.answer() при регистрации нескольких handlers. Поэтому в проекте
одна точка входа — `dispatch_inline`, а модули предоставляют чистые
async-функции, которые вызываются из dispatcher'а напрямую.
"""

import time as _time

from aiogram import Bot, Router, types

from config import INLINE_CACHE_DEFAULT


_BOT_START_TS: float = 0.0


def mark_bot_started() -> None:
    """`bot.py::on_startup` вызывает эту функцию, чтобы зафиксировать timing uptime."""
    global _BOT_START_TS
    if _BOT_START_TS == 0.0:
        _BOT_START_TS = _time.time()


def uptime_seconds() -> float:
    if not _BOT_START_TS:
        return 0.0
    return _time.time() - _BOT_START_TS


router = Router(name="inline")


def connect_button(
    text: str = "[+] Подключить бота",
    parameter: str = "start",
) -> types.InlineQueryResultsButton:
    return types.InlineQueryResultsButton(text=text, start_parameter=parameter)


from . import help as _help_mod      # noqa: E402
from . import status as _status_mod  # noqa: E402
from . import profile as _profile_mod  # noqa: E402


@router.inline_query()
async def dispatch_inline(inline: types.InlineQuery, bot: Bot):
    """Single entry-point для всех inline-запросов бота.

    Решает HIGH-проблему из code-review: в aiogram 3.x dispatcher
    прогоняет всех matched handlers параллельно — если бы каждый
    submodule регистрировал свой @router.inline_query(...), Telegram
    получал бы двойной inline.answer() → BadRequest.
    """
    q = (inline.query or "").strip()
    if q.startswith("@"):
        # `@help` / `@status` — это команды, а не username для резолва.
        bare = q[1:].strip().lower()
        if _help_mod.is_help_query(bare):
            return await _help_mod.handle(inline)
        if _status_mod.is_status_query(bare):
            return await _status_mod.handle(inline, bot)
        return await _profile_mod.handle(inline)
    if _help_mod.is_help_query(q):
        return await _help_mod.handle(inline)
    if _status_mod.is_status_query(q):
        return await _status_mod.handle(inline, bot)
    await inline.answer(
        results=[],
        cache_time=INLINE_CACHE_DEFAULT,
        is_personal=True,
    )
