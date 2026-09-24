"""Определение Telegram Premium статуса для рендера премиум-эмодзи.

Custom-эмодзи (``<tg-emoji emoji-id="...">fallback</tg-emoji>``) рендерятся
анимированными ТОЛЬКО если у ОТПРАВИТЕЛЯ сообщения есть Telegram Premium:

- В Telethon — у самого пользователя (``client.get_me().premium``).
- В aiogram через обычный чат с ботом — бот не может иметь Premium, эмодзи
  всегда отрисуются как fallback unicode.

Этот модуль инкапсулирует логику определения Premium-статуса с кэшированием
(Telethon ``get_me()`` не дёшев, не дёргаем его на каждом сообщении).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Кэш Premium-статуса по user_id.
# Значение: (premium: bool, fetched_at: float).
# TTL = 5 минут — Premium-статус меняется крайне редко (только при покупке/
# отмене подписки), 5 мин достаточно. Кэш инвалидируется явно при logout.
_PREMIUM_CACHE: dict[str, tuple[bool, float]] = {}
_CACHE_TTL = 300.0  # секунд
_CACHE_LOCKS: dict[str, asyncio.Lock] = {}


def _cache_key(user_id: int | str | None) -> Optional[str]:
    if user_id is None:
        return None
    return str(user_id)


def _cache_get(uid: str) -> Optional[bool]:
    """Возвращает кэшированное значение или None, если кэш пуст/протух."""
    entry = _PREMIUM_CACHE.get(uid)
    if entry is None:
        return None
    val, ts = entry
    if time.time() - ts > _CACHE_TTL:
        _PREMIUM_CACHE.pop(uid, None)
        return None
    return val


def _cache_set(uid: str, val: bool) -> None:
    _PREMIUM_CACHE[uid] = (bool(val), time.time())


def invalidate_premium_cache(user_id: int | str | None = None) -> None:
    """Сбрасывает кэш Premium.

    Args:
        user_id: если указан — сбрасывает только для этого юзера;
                 если None — сбрасывает весь кэш.
    """
    if user_id is None:
        _PREMIUM_CACHE.clear()
        _CACHE_LOCKS.clear()
        return
    key = _cache_key(user_id)
    if key is not None:
        _PREMIUM_CACHE.pop(key, None)
        _CACHE_LOCKS.pop(key, None)


async def is_user_premium(user_id: int | str | None) -> bool:
    """Определяет, есть ли у пользователя Telegram Premium.

    Стратегия:
    1. Сначала проверяем кэш (``_CACHE_TTL`` = 5 минут).
    2. Если есть активная Telethon-сессия (``telethon_manager.get_client``) →
       ``await client.get_me()`` → ``.premium``.
    3. Иначе → False.

    Per-user asyncio.Lock защищает от «грозового стада» (thundering herd):
    если несколько сообщений приходят одновременно и кэш пуст, только
    первый вызов делает Telethon-запрос, остальные ждут его результат.
    """
    key = _cache_key(user_id)
    if key is None:
        return False

    cached = _cache_get(key)
    if cached is not None:
        return cached

    # Per-user lock, чтобы concurrent вызовы для одного uid не дублировали
    # get_me().
    lock = _CACHE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CACHE_LOCKS[key] = lock

    async with lock:
        # Повторная проверка под локом — первый вызов мог уже заполнить кэш.
        cached = _cache_get(key)
        if cached is not None:
            return cached

        # Lazy import — избегаем circular import (utils.telethon_manager
        # импортирует handlers/commands/*, те импортируют utils.texts,
        # и если texts импортирует premium на module load — петля).
        from utils.telethon_manager import telethon_manager

        client = telethon_manager.get_client(key)
        if client is None:
            _cache_set(key, False)
            return False

        try:
            me = await client.get_me()
            premium = bool(getattr(me, "premium", False))
            _cache_set(key, premium)
            return premium
        except Exception as e:
            logger.debug("is_user_premium(%s): get_me failed: %s", key, e)
            # На ошибке считаем как False (fallback на unicode), не кэшируем —
            # следующий вызов попробует ещё раз.
            return False
        finally:
            # _CACHE_LOCKS растёт на каждый новый uid и никогда не чистится
            # (кэш TTL-ится, локи — нет). После завершения загрузки лок не
            # нужен: кэш живёт 5 минут, а гонка «два одновременных get_me»
            # при протухшем кэше — редкая и дешёвая.
            _CACHE_LOCKS.pop(key, None)


def is_entity_premium(entity: Any) -> bool:
    """Определяет Premium из уже-загруженной сущности (без I/O).

    Работает с обоими типами:
    - Telethon ``telethon.tl.types.User`` → атрибут ``.premium``
    - aiogram ``aiogram.types.User``    → атрибут ``.is_premium``
    """
    if entity is None:
        return False
    return bool(
        getattr(entity, "premium", getattr(entity, "is_premium", False))
    )


def is_aiogram_message_premium(message: Any) -> bool:
    """Premium из aiogram ``message.from_user.is_premium`` (личка с ботом)."""
    user = getattr(message, "from_user", None)
    return is_entity_premium(user)


async def resolve_effective_uid(message: Any) -> Optional[str]:
    """UID отправителя для aiogram ``Message`` (личка с ботом).

    Returns:
        UID (``str``) или ``None`` если не удалось определить.
    """
    user = getattr(message, "from_user", None)
    if user is not None:
        uid = getattr(user, "id", None)
        if uid is not None:
            return str(uid)
    return None


__all__ = [
    "is_user_premium",
    "is_entity_premium",
    "is_aiogram_message_premium",
    "resolve_effective_uid",
    "invalidate_premium_cache",
]
