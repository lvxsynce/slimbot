"""Inline-режим @slimbot: точка сборки и единый dispatcher.

Доступные команды (диспатчер определяет по `InlineQuery.query`):
- пустой / помощь / help         → handlers/inline/help.py:handle
- статус / status                → handlers/inline/status.py:handle
- @username                      → handlers/inline/profile.py:handle
- что угодно другое              → handlers/inline/help.py:handle_hint

Архитектурный note: в aiogram 3.x dispatcher прогоняет ВСЕ observers,
чьи фильтры прошли — для inline_query это привело бы к двойному
inline.answer() при регистрации нескольких handlers. Поэтому в проекте
одна точка входа — `dispatch_inline`, а модули предоставляют чистые
async-функции, которые вызываются из dispatcher'а напрямую.
"""

import logging
import time as _time

from aiogram import Bot, Router, types

from config import INLINE_CACHE_DEFAULT


_log = logging.getLogger(__name__)

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
    if _profile_mod.is_profile_query(q):
        # `@help` / `@status` — это команды, а не username для резолва.
        # `bare` проверяем на непустоту: иначе голое `@` (юзер набрал
        # «@bot @» и ждёт подсказку с @username) ушло бы в справку,
        # и `profile._hint_text()` остался бы недостижимым.
        bare = q[1:].strip().lower()
        if bare:
            if _help_mod.is_help_query(bare):
                return await _help_mod.handle(inline)
            if _status_mod.is_status_query(bare):
                return await _status_mod.handle(inline, bot)
        return await _profile_mod.handle(inline)
    if _help_mod.is_help_query(q):
        return await _help_mod.handle(inline)
    if _status_mod.is_status_query(q):
        return await _status_mod.handle(inline, bot)
    # Раньше здесь был `results=[]`: юзер видел пустой список и не понимал,
    # что бот умеет. Теперь — карточка со списком inline-команд.
    if await _try_module_inline(inline, q):
        return
    return await _help_mod.handle_hint(inline)


async def _try_module_inline(inline: types.InlineQuery, q: str) -> bool:
    """Отдать запрос inline-команде модуля. True — модуль ответил.

    Ключ ищется по ПЕРВОМУ слову запроса, поэтому `@bot заметка купить
    хлеб` попадёт в модуль с ключом `заметка`, а остаток придёт в
    `ctx.args`. Так же устроены и встроенные команды (`статус`).

    Модуль может вернуть `None`, если не хочет отвечать (например,
    требует сессии) — тогда сработает встроенная справка.
    """
    from utils import modules as _modules

    if not _modules.modules_enabled():
        return False
    uid = str(inline.from_user.id)
    if not _modules.module_allowed(uid):
        return False

    key = q.split(maxsplit=1)[0].lower() if q else ""
    if not key or key.startswith("@"):
        return False
    spec = _modules.find_inline(uid, key)
    if spec is None:
        return False

    from utils.module_state import ModuleState

    module = _modules.module_of_inline(user_id=uid, spec=spec)
    args = q[len(key):].strip()
    ctx = _modules.Context(
        user_id=uid,
        args=args,
        argv=args.split(),
        head=key,
        event=inline,
        client=inline.bot,
        module=module,
        meta={
            "name": module.name if module else "",
            "version": module.version if module else "",
            "description": module.description if module else "",
        },
        state=ModuleState(uid, module.name if module else key),
    )
    try:
        results = await _modules.run_inline(uid, spec, ctx)
    except Exception:
        _log.exception("modules: inline %s failed (uid=%s)", key, uid)
        return False
    if not results:
        return False
    await inline.answer(results=list(results), cache_time=INLINE_CACHE_DEFAULT,
                        is_personal=True)
    return True