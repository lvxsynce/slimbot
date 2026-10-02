"""Rate limiter: корректность `clear()` и отсутствие неограниченного роста.

Регрессия: `clear()` сравнивал `key[0] == prefix`, где `key` уже был кортежем
`(user_id, operation)`. Сравнение «строка == кортеж» всегда False ⇒ на logout
bucket'ы НЕ удалялись, и лимиты переживали переподключение.
"""

import time

import pytest

from utils import rate_limit


@pytest.fixture(autouse=True)
def _reset():
    rate_limit.reset()
    yield
    rate_limit.reset()


def test_allows_up_to_limit():
    for _ in range(3):
        assert rate_limit.allow("u1", "ai", limit=3, window=60) is True
    assert rate_limit.allow("u1", "ai", limit=3, window=60) is False


def test_window_expiry_allows_again():
    assert rate_limit.allow("u1", "ai", limit=1, window=0.01) is True
    assert rate_limit.allow("u1", "ai", limit=1, window=0.01) is False
    time.sleep(0.02)
    assert rate_limit.allow("u1", "ai", limit=1, window=0.01) is True


def test_operations_are_independent():
    assert rate_limit.allow("u1", "ai", limit=1, window=60) is True
    assert rate_limit.allow("u1", "network", limit=1, window=60) is True


def test_users_are_independent():
    assert rate_limit.allow("u1", "ai", limit=1, window=60) is True
    assert rate_limit.allow("u2", "ai", limit=1, window=60) is True
    assert rate_limit.allow("u1", "ai", limit=1, window=60) is False


def test_clear_actually_removes_user_buckets():
    """Ядро регрессии: до фикса clear() был no-op."""
    assert rate_limit.allow("u1", "ai", limit=1, window=60) is True
    assert rate_limit.allow("u1", "network", limit=1, window=60) is True
    assert rate_limit.bucket_count() == 2

    rate_limit.clear("u1")
    assert rate_limit.bucket_count() == 0, "clear() must remove every bucket of the user"

    # и лимит снова доступен
    assert rate_limit.allow("u1", "ai", limit=1, window=60) is True


def test_clear_does_not_touch_other_users():
    rate_limit.allow("u1", "ai", limit=1, window=60)
    rate_limit.allow("u2", "ai", limit=1, window=60)
    rate_limit.clear("u1")
    assert rate_limit.allow("u2", "ai", limit=1, window=60) is False
    assert rate_limit.allow("u1", "ai", limit=1, window=60) is True


def test_clear_accepts_int_uid():
    """logout приходит с uid, который в других местах приходит как str."""
    rate_limit.allow("42", "ai", limit=1, window=60)
    rate_limit.clear(42)
    assert rate_limit.bucket_count() == 0


def test_sweep_removes_stale_buckets(monkeypatch):
    monkeypatch.setattr(rate_limit, "_SWEEP_AGE", 0.01)
    rate_limit.allow("u1", "ai", limit=1, window=60)
    rate_limit.allow("u2", "ai", limit=1, window=60)
    assert rate_limit.bucket_count() == 2
    time.sleep(0.02)
    # Форсируем свип: allow() дёргает его только по своему расписанию, поэтому
    # вызываем напрямую — это чистая функция над состоянием.
    rate_limit._sweep(time.monotonic())
    assert rate_limit.bucket_count() == 0, "stale buckets must be swept"


def test_sweep_keeps_fresh_buckets(monkeypatch):
    monkeypatch.setattr(rate_limit, "_SWEEP_AGE", 60.0)
    rate_limit.allow("u1", "ai", limit=1, window=60)
    rate_limit._sweep(time.monotonic())
    assert rate_limit.bucket_count() == 1


def test_allow_triggers_sweep_on_interval(monkeypatch):
    """allow() сам вызывает свип по расписанию — иначе словарь рос бы вечно."""
    monkeypatch.setattr(rate_limit, "_SWEEP_AGE", 0.0)
    monkeypatch.setattr(rate_limit, "_SWEEP_INTERVAL", 0.0)
    rate_limit.allow("u1", "ai", limit=1, window=3600)
    # Свип отрабатывает ДО вставки, поэтому первый вызов его ещё не видит.
    # Второй вызов обязан запустить свип и вычистить первый bucket.
    rate_limit.allow("u2", "ai", limit=1, window=3600)
    assert rate_limit.bucket_count() <= 1, rate_limit.bucket_count()


def test_hard_cap_bounds_memory(monkeypatch):
    monkeypatch.setattr(rate_limit, "_MAX_BUCKETS", 50)
    for i in range(500):
        rate_limit.allow(f"u{i}", "ai", limit=1, window=3600)
    assert rate_limit.bucket_count() <= 50, rate_limit.bucket_count()


def test_expired_entries_do_not_grow_bucket_len():
    bucket = rate_limit._BUCKETS[("u1", "ai")]
    for _ in range(10):
        rate_limit.allow("u1", "ai", limit=2, window=0.01)
        time.sleep(0.011)
    assert len(bucket) <= 2, len(bucket)
