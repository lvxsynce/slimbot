import logging

from aiogram import Router, types
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
_BYPASS_HEAD = ()


def _bypass(text: str) -> bool:
    t = text.strip().lower()
    if t in _BYPASS_FULL:
        return True
    head = t.split(maxsplit=1)[0]
    return head in _BYPASS_HEAD


def _head(text: str) -> str:
    return text.strip().split(maxsplit=1)[0].lower()


def _suggest_hint(text: str) -> str | None:
    from utils.suggest import suggest_text
    return suggest_text(_head(text))


@router.message(
    lambda msg: msg.text
    and msg.text.strip().startswith(".")
    and not _bypass(msg.text)
    and not _is_help_request(msg.text)
    and (not msg.from_user or not session_exists(str(msg.from_user.id)))
)
async def ignore_unauthorized(message: types.Message):
    hint = _suggest_hint(message.text)
    if hint:
        await message.reply(hint, **thread_kwargs(message))


from . import cmdhelp, dm, extra, help, id, love, ping, start, status, time, timezone, watch, logout, tools, netcmds, admins, pin, coin, knowledge, ai, opencode

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
# admins / pin / invitelink / delmsg / dm / tagall / nya / vgf / quote —
# Telethon-only модули (без aiogram Router), вызываются через telethon_manager._handle_outgoing


def _is_unknown_dot(msg: types.Message) -> bool:
    if not msg.text or not msg.text.strip().startswith("."):
        return False
    if _bypass(msg.text) or _is_help_request(msg.text):
        return False
    return _suggest_hint(msg.text) is not None


@router.message(_is_unknown_dot)
async def suggest_unknown_private(message: types.Message):
    """Fallback: в личке с ботом (Telethon не обрабатывает чат с ботом).
    Срабатывает только если ни один command-роутер не подошёл."""
    uid = str(message.from_user.id) if message.from_user else ""
    if not uid or not session_exists(uid):
        return
    await message.reply(_suggest_hint(message.text), **thread_kwargs(message))
