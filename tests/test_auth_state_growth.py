"""P3.12 — auth-словари не растут бесконечно.

`_AUTH_LOCKS` — `defaultdict(asyncio.Lock)`, а он СОЗДАЁТ запись на
каждом чтении. Три вызова `_AUTH_LOCKS[uid]` на каждый запрос ⇒ по одному
Lock на каждого юзера, который хоть раз коснулся аутентификации, и запись
жила до конца процесса. Теперь чтение идёт через `_auth_lock()`, который
отмечает активность в `_AUTH_SEEN`, и оба словаря чистятся вместе.
"""

import asyncio
import time

import pytest

from handlers import session as S


@pytest.fixture(autouse=True)
def _clean():
    saved = {
        "seen": dict(S._AUTH_SEEN),
        "locks": dict(S._AUTH_LOCKS),
        "att": {k: list(v) for k, v in S._AUTH_ATTEMPTS.items()},
    }
    yield
    S._AUTH_SEEN.clear(); S._AUTH_SEEN.update(saved["seen"])
    S._AUTH_LOCKS.clear(); S._AUTH_LOCKS.update(saved["locks"])
    S._AUTH_ATTEMPTS.clear()
    for k, v in saved["att"].items():
        S._AUTH_ATTEMPTS[k] = __import__("collections").deque(v)


def test_take_lock_creates_one():
    async def go():
        a = S._auth_lock("u1")
        b = S._auth_lock("u1")
        assert a is b, "the same user must get the same lock"

    asyncio.run(go())
    assert len([k for k in S._AUTH_LOCKS if k == "u1"]) == 1


def test_touch_records_activity():
    # _auth_lock — синхронная функция (возвращает Lock, а не await'ит его)
    S._auth_lock("u1")
    assert "u1" in S._AUTH_SEEN
    assert time.monotonic() - S._AUTH_SEEN["u1"] < 5


def test_prune_drops_stale_locks():
    old = time.monotonic() - (S.AUTH_ATTEMPT_WINDOW + 100)
    S._AUTH_SEEN["stale"] = old
    S._AUTH_LOCKS["stale"] = asyncio.Lock()
    S._AUTH_ATTEMPTS["stale"] = __import__("collections").deque([old])
    removed = S._prune_auth_state(time.monotonic())
    assert removed >= 2
    assert "stale" not in S._AUTH_LOCKS
    assert "stale" not in S._AUTH_ATTEMPTS
    assert "stale" not in S._AUTH_SEEN


def test_prune_keeps_fresh():
    now = time.monotonic()
    S._AUTH_SEEN["live"] = now
    S._AUTH_LOCKS["live"] = asyncio.Lock()
    S._AUTH_ATTEMPTS["live"] = __import__("collections").deque([now])
    S._prune_auth_state(now)
    assert "live" in S._AUTH_LOCKS


def test_prune_cleans_attempts_without_seen():
    """Запись, попавшая в _AUTH_ATTEMPTS мимо _AUTH_SEEN, тоже вычищается."""
    now = time.monotonic()
    stale = now - (S.AUTH_ATTEMPT_WINDOW + 10)
    S._AUTH_ATTEMPTS["orphan"] = __import__("collections").deque([stale])
    S._prune_auth_state(now)
    assert "orphan" not in S._AUTH_ATTEMPTS


def test_growth_is_bounded_under_flood():
    """2500 разных uid не должны оставить 2500 записей."""
    now = time.monotonic()
    for i in range(2500):
        S._AUTH_SEEN[f"u{i}"] = now
    S._allow_auth_attempt("fresh")
    biggest = max(
        len(S._AUTH_SEEN), len(S._AUTH_ATTEMPTS), len(S._AUTH_LOCKS)
    )
    assert biggest <= S._AUTH_PRUNE_KEEP + 10, biggest


def test_thresholds_cover_all_three_dicts():
    """Порог обязан учитывать все словари, а не только _AUTH_SEEN."""
    import inspect

    src = inspect.getsource(S._touch_auth_state)
    assert "len(_AUTH_SEEN)" in src
    assert "len(_AUTH_ATTEMPTS)" in src
    assert "len(_AUTH_LOCKS)" in src


def test_auth_lock_is_the_only_direct_reader():
    """Прямых `_AUTH_LOCKS[uid]` в коде быть не должно (кроме определения)."""
    import ast
    import inspect
    import pathlib
    import re

    src = pathlib.Path(S.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    direct = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "_AUTH_LOCKS"
        ):
            direct.append(node.lineno)
    # допускается ровно одно чтение — внутри _auth_lock()
    assert len(direct) <= 1, f"direct _AUTH_LOCKS[...] reads at lines {direct}"
