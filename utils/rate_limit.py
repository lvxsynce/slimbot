"""Small in-memory rate limiter for expensive user-triggered operations."""

import time
from collections import defaultdict, deque


_BUCKETS: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def allow(key: str, operation: str, *, limit: int, window: float) -> bool:
    now = time.monotonic()
    bucket = _BUCKETS[(str(key), operation)]
    while bucket and now - bucket[0] >= window:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def clear(key: str) -> None:
    prefix = str(key)
    for bucket_key in [key for key in _BUCKETS if key[0] == prefix]:
        _BUCKETS.pop(bucket_key, None)
