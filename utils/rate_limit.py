"""In-memory rate limiter for expensive user-triggered operations.

Bounded by construction: buckets are `(user_id, operation)` pairs and every
`allow()` call evicts the entries whose window expired. A background sweep is
still needed for the case where a user goes silent mid-window — otherwise
`_BUCKETS` grows without bound across many accounts.
"""

import time
from collections import defaultdict, deque


_BUCKETS: dict[tuple[str, str], deque[float]] = defaultdict(deque)
#: Evict buckets untouched for longer than this. Comfortably above any
#: configured window (60s), so a live bucket is never swept out from under
#: its own window check.
_SWEEP_AGE = 3600.0
_last_sweep = 0.0
#: Sweep cadence and hard cap, so the dict cannot grow unbounded even if
#: ``allow()`` is never called again (e.g. bot idles after a traffic burst).
_SWEEP_INTERVAL = 300.0
_MAX_BUCKETS = 20_000


def _sweep(now: float) -> None:
    """Drop buckets whose last activity is older than ``_SWEEP_AGE``."""
    global _last_sweep
    _last_sweep = now
    cutoff = now - _SWEEP_AGE
    stale = [k for k, bucket in _BUCKETS.items() if not bucket or bucket[-1] < cutoff]
    for key in stale:
        _BUCKETS.pop(key, None)

    if len(_BUCKETS) > _MAX_BUCKETS:
        # Hard cap: keep the most recently active buckets.
        ordered = sorted(_BUCKETS.items(), key=lambda kv: (kv[1][-1] if kv[1] else 0), reverse=True)
        for key, _ in ordered[_MAX_BUCKETS:]:
            _BUCKETS.pop(key, None)


def allow(key: str, operation: str, *, limit: int, window: float) -> bool:
    global _last_sweep
    now = time.monotonic()
    if now - _last_sweep >= _SWEEP_INTERVAL or len(_BUCKETS) > _MAX_BUCKETS:
        _sweep(now)
    bucket = _BUCKETS[(str(key), operation)]
    while bucket and now - bucket[0] >= window:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    # Cap проверяем ПОСЛЕ вставки: до вставки размер всегда <= cap, и словарь
    # рос бы на один элемент за вызов до следующего свипа.
    if len(_BUCKETS) > _MAX_BUCKETS:
        _sweep(now)
    return True


def clear(key: str) -> None:
    """Сбросить все bucket'ы пользователя (вызывается на logout).

    Ключи — кортежи ``(user_id, operation)``, поэтому сравнивать надо первый
    элемент кортежа, а не сам ключ. Раньше здесь стояло ``key[0] == prefix``,
    где ``key`` уже был кортежем: сравнение строка-кортеж всегда давало False,
    и ``clear()`` не удалял ничего — лимиты переживали logout.
    """
    prefix = str(key)
    for bucket_key in [k for k in _BUCKETS if k[0] == prefix]:
        _BUCKETS.pop(bucket_key, None)


def bucket_count() -> int:
    """Размер внутреннего словаря — используется в тестах на утечки."""
    return len(_BUCKETS)


def reset() -> None:
    """Полный сброс (тесты)."""
    global _last_sweep
    _BUCKETS.clear()
    _last_sweep = 0.0
