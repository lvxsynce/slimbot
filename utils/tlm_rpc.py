"""HTTP/MTProto клиенты: единый пул сессий + таймауты на каждый RPC.

Зачем
----
Telethon держит соединение и для long-polling, и для RPC. Один клиент на
юзера — стандарт, но каждый его RPC обязан быть ограничен по времени:
иначе один зависший сокет держит per-user lock, и все команды этого юзера
(включая `.love`, где это после 21 edit'а) встают в очередь.

Также здесь единые обёртки для `get_entity` / `get_me`, чтобы таймаут
нельзя было «забыть» в новом хендлере (регрессия P3.6).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config import TELETHON_RESOLVE_TIMEOUT, TELETHON_SEND_TIMEOUT

logger = logging.getLogger(__name__)

#: Таймаут дешёвых read-RPC (get_entity, get_me, iter_messages-первый).
RESOLVE_TIMEOUT = TELETHON_RESOLVE_TIMEOUT
#: Таймаут отправки (send_message/send_file) — она может ждать upload.
SEND_TIMEOUT = TELETHON_SEND_TIMEOUT


class TelethonTimeout(RuntimeError):
    """RPC не уложился в таймаут."""


async def with_timeout(awaitable, timeout: float = RESOLVE_TIMEOUT, *, what: str = "rpc"):
    """Обернуть awaitable в таймаут с внятной ошибкой.

    ``asyncio.wait_for`` поднимает голый ``TimeoutError``, из которого
    непонятно, что именно зависло. Здесь сообщение содержит имя операции.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as e:
        raise TelethonTimeout(f"{what}: таймаут {timeout}с") from e


async def resolve(client, target, timeout: float = RESOLVE_TIMEOUT):
    """``client.get_entity`` под таймаутом. ``None`` при любой ошибке.

    Единая точка: раньше часть вызовов оборачивалась в wait_for, часть — нет,
    и забытый вызов висел до внешнего 300-секундного таймаута хендлера.
    """
    if client is None:
        return None
    try:
        return await with_timeout(
            client.get_entity(target), timeout, what=f"get_entity({target!r})"
        )
    except Exception as e:
        logger.debug("resolve %r failed: %s", target, e)
        return None


async def get_me(client, timeout: float = RESOLVE_TIMEOUT):
    """``client.get_me()`` под таймаутом."""
    if client is None:
        return None
    try:
        return await with_timeout(client.get_me(), timeout, what="get_me")
    except Exception as e:
        logger.debug("get_me failed: %s", e)
        return None


async def send(client, coro, timeout: float = SEND_TIMEOUT):
    """Отправка (send_message/send_file) под таймаутом.

    ``coro`` — уже созданная корутина; таймаут нужен, потому что upload
    медиа может висеть дольше обычного.
    """
    return await with_timeout(coro, timeout, what="send")


def is_authorized(client) -> bool:
    """Синхронная проверка (кэшированная на стороне Telethon)."""
    if client is None:
        return False
    try:
        return bool(client.is_authorized())
    except Exception:
        return False


def connected(client) -> bool:
    if client is None:
        return False
    try:
        return bool(client.is_connected())
    except Exception:
        return False


__all__ = [
    "RESOLVE_TIMEOUT",
    "SEND_TIMEOUT",
    "TelethonTimeout",
    "with_timeout",
    "resolve",
    "get_me",
    "send",
    "is_authorized",
    "connected",
]
