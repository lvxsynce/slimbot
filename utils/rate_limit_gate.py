"""Единая точка rate-limit для Telethon- и aiogram-путей.

Зачем
----
Бюджеты описаны в `utils.cmds` (что ограничиваем), а лимиты — в `config`
(сколько раз). Здесь они соединяются, чтобы оба пути вызывали ОДНУ функцию
и не могли разойтись — именно так раньше `.love` оказался под лимитом в
Telethon-пути и без лимита в aiogram.

Также бюджеты РАЗДЕЛЬНЫЕ. Общий «network» на всё тяжёлое позволял пяти
дешёвым `.love` заблокировать `.net` на минуту.
"""

from __future__ import annotations

from config import (
    AI_REQUEST_LIMIT,
    AI_REQUEST_WINDOW,
    EXPENSIVE_COMMAND_LIMIT,
    EXPENSIVE_COMMAND_WINDOW,
)
from utils.cmds import (
    ANIM_BUDGET,
    AI_BUDGET,
    HEAVY_BUDGET,
    LOCAL_BUDGET,
    NETWORK_BUDGET,
)

#: head → (операция, лимит, окно). Операция попадает в ключ бакета, поэтому
#: разные бюджеты не мешают друг другу.
BUDGETS: dict[str, tuple[str, int, float]] = {}

for _heads, _op, _limit, _window in (
    (AI_BUDGET, "ai", AI_REQUEST_LIMIT, AI_REQUEST_WINDOW),
    (NETWORK_BUDGET, "network", EXPENSIVE_COMMAND_LIMIT, EXPENSIVE_COMMAND_WINDOW),
    (HEAVY_BUDGET, "heavy", EXPENSIVE_COMMAND_LIMIT, EXPENSIVE_COMMAND_WINDOW),
    (ANIM_BUDGET, "anim", EXPENSIVE_COMMAND_LIMIT, EXPENSIVE_COMMAND_WINDOW),
    (LOCAL_BUDGET, "local", EXPENSIVE_COMMAND_LIMIT, EXPENSIVE_COMMAND_WINDOW),
):
    for _head in _heads:
        BUDGETS[_head] = (_op, _limit, _window)

del _heads, _op, _limit, _window


def rate_limit_for(head: str) -> tuple[str, int, float] | None:
    """Бюджет для команды, либо None если она не ограничена."""
    return BUDGETS.get(str(head or "").strip().lower())


def check(head: str, user_id) -> bool:
    """True = можно выполнять; False = превышен лимит.

    Единственная точка входа для обоих путей.
    """
    budget = rate_limit_for(head)
    if budget is None:
        return True
    operation, limit, window = budget
    from utils.rate_limit import allow
    return allow(user_id, operation, limit=limit, window=window)


def is_limited(head: str) -> bool:
    return rate_limit_for(head) is not None


RATE_LIMIT_TEXT = "[x] Слишком много запросов. Подожди немного."

__all__ = [
    "BUDGETS",
    "RATE_LIMIT_TEXT",
    "check",
    "is_limited",
    "rate_limit_for",
]
