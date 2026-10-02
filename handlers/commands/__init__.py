import logging

from aiogram import Router, types
from aiogram.dispatcher.event.bases import SkipHandler
from utils.storage import session_exists

from . import _helpdb
from ._base import thread_kwargs

logger = logging.getLogger(__name__)

router = Router()

# .cmd справка / .cmd help — никогда не блокируем (это справка)
def _is_help_request(text: str | None) -> bool:
    ok, _ = _helpdb.is_help_request(text)
    return ok


_BYPASS_FULL = (".help", ".помощь")


def _bypass(text: str) -> bool:
    """`.help` / `.помощь` целиком — единственное, что не должно уходить
    в fallback-подсказку.

    Раньше здесь был ещё `_BYPASS_HEAD` (пустой кортеж) и мёртвая проверка
    head-токена — удалены (P4.5)."""
    return text.strip().lower() in _BYPASS_FULL


def _head(text: str) -> str:
    return text.strip().split(maxsplit=1)[0].lower()


def _suggest_hint(text: str) -> str | None:
    from utils.suggest import suggest_text
    return suggest_text(_head(text))


def _is_dot(text: str | None) -> bool:
    return bool(text and text.strip().startswith("."))


from . import cmdhelp, dm, extra, help, id, love, ping, start, status, time, timezone, watch, logout, tools, netcmds, admins, pin, coin, knowledge, ai, opencode, modules

# cmdhelp ПЕРВЫМ – перехватывает `.cmd справка` для всех команд
router.include_router(cmdhelp.router)
router.include_router(extra.router)
router.include_router(help.router)
router.include_router(id.router)
router.include_router(love.router)
router.include_router(logout.router)
router.include_router(netcmds.router)
router.include_router(ping.router)
router.include_router(start.router)
router.include_router(status.router)
router.include_router(time.router)
router.include_router(timezone.router)
router.include_router(tools.router)
router.include_router(watch.router)
router.include_router(coin.router)
router.include_router(knowledge.router)
router.include_router(ai.router)
router.include_router(opencode.router)
# Модули юзера: /modules (установка и управление) + dot-команды модулей
# в личке с ботом. Должен идти ПОСЛЕ всех системных роутеров (модуль не
# должен перебивать встроенную команду без явного решения по конфликту) и
# ДО `_fallback_router` (иначе fallback ответил бы подсказкой вместо модуля).
router.include_router(modules.router)
# admins / pin / invitelink / delmsg / dm / tagall / nya / vgf / quote / template —
# Telethon-only модули (без aiogram Router), вызываются через telethon_manager._handle_outgoing


# ---------------------------------------------------------------------------
# Fallback-подсказки — в ОТДЕЛЬНОМ суб-роутере, подключаемом ПОСЛЕДНИМ.
#
# Почему их больше нельзя держать на `router` (как было раньше):
# в aiogram 3.x `Router._propagate_event` сначала проверяет СВОИ хендлеры и,
# если хотя бы один вернул не-UNHANDLED, sub_routers НЕ обходятся. Старый
# `ignore_unauthorized` возвращал `None` (подсказка не нашлась) — этого
# достаточно, чтобы глушить ВСЕ dot-команды для юзеров без Telethon-сессии.
# Отдельный суб-роутер + SkipHandler закрывают оба возможных обхода.
# ---------------------------------------------------------------------------
_fallback_router = Router(name="commands-fallback")


def _is_unknown_dot(msg: types.Message) -> bool:
    """Сообщение — заведомо опечатка (есть близкий известный алиас)."""
    if not _is_dot(msg.text):
        return False
    if _bypass(msg.text) or _is_help_request(msg.text):
        return False
    return _suggest_hint(msg.text) is not None


@_fallback_router.message(
    lambda msg: _is_dot(msg.text)
    and not _bypass(msg.text)
    and not _is_help_request(msg.text)
    and (not msg.from_user or not session_exists(str(msg.from_user.id)))
)
async def ignore_unauthorized(message: types.Message):
    """Юзер БЕЗ Telethon-сессии.

    aiogram-слой работает и без сессии, поэтому команды НЕ блокируются.
    Наша задача — подсказать, если это опечатка. Если подсказки нет,
    отдаём обработку дальше через SkipHandler (иначе aiogram посчитает
    хендлер сработавшим и не дойдёт до остальных роутеров)."""
    hint = _suggest_hint(message.text)
    if not hint:
        raise SkipHandler()
    await message.reply(hint, **thread_kwargs(message))


@_fallback_router.message(_is_unknown_dot)
async def suggest_unknown_private(message: types.Message):
    """Fallback: в личке с ботом (Telethon не обрабатывает чат с ботом).
    Срабатывает только если ни один command-роутер не подошёл."""
    uid = str(message.from_user.id) if message.from_user else ""
    if not uid or not session_exists(uid):
        raise SkipHandler()
    await message.reply(_suggest_hint(message.text), **thread_kwargs(message))


# ПОСЛЕДНИМ — до этого момента ни один command-роутер не ответил.
router.include_router(_fallback_router)
