"""P1.2 — `was_processed` обязан различать пользователей.

Баг: ключ был ``(chat_id, msg_id, thread_id)`` без ``user_id``, а словарь
_processed_msgs — process-global. Два юзера, подключившие один чат через
``.watch``, делили ключ, и второй молча терял одноразовое фото.
"""

import pytest

from utils.storage import _processed_msgs, was_processed


@pytest.fixture(autouse=True)
def _clean_guard():
    _processed_msgs.clear()
    yield
    _processed_msgs.clear()


def test_same_event_is_processed_once_per_user():
    assert was_processed(-100777, 4242, 0, 111) is False
    assert was_processed(-100777, 4242, 0, 111) is True


def test_different_users_do_not_suppress_each_other():
    """Ядро фикса: второй юзер в том же чате обязан получить своё событие."""
    assert was_processed(-100777, 4242, 0, 111) is False
    assert was_processed(-100777, 4242, 0, 222) is False, (
        "second user must not be suppressed by the first user's marker"
    )
    # и каждый из них по-прежнему дедуплицирует для себя
    assert was_processed(-100777, 4242, 0, 111) is True
    assert was_processed(-100777, 4242, 0, 222) is True


def test_same_user_different_chats_are_independent():
    assert was_processed(1, 1, 0, 111) is False
    assert was_processed(2, 1, 0, 111) is False
    assert was_processed(1, 1, 0, 111) is True


def test_thread_awareness_is_preserved():
    assert was_processed(5, 5, 1, 111) is False
    assert was_processed(5, 5, 1, 111) is True
    assert was_processed(5, 5, 2, 111) is False


def test_key_contains_user_id_component():
    was_processed(9, 9, 0, 777)
    keys = [k for k in _processed_msgs if k[0] == 9 and k[1] == 9]
    assert keys, "no marker registered"
    assert keys[0][3] == "777", f"user component missing from key: {keys[0]!r}"


def test_user_id_none_keeps_legacy_behaviour():
    """Явный None (aiogram-путь без uid) — всё ещё работает, но в своей нише."""
    assert was_processed(3, 3, 0) is False
    assert was_processed(3, 3, 0) is True
    # и не конфликтует с реальным uid
    assert was_processed(3, 3, 0, 111) is False


def test_all_call_sites_pass_user_id():
    """Статический инвариант: ни один вызов в проде не остался без user_id.

    Telethon-пути обязаны передавать uid — иначе баг вернётся.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    pattern = re.compile(r"was_processed\((?:[^()]|\([^()]*\))*\)", re.S)
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        if path.name == "storage.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            call = match.group(0)
            # 4-й позиционный аргумент = user_id
            inner = call[len("was_processed("):-1]
            depth = 0
            parts, cur = [], ""
            for ch in inner:
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                if ch == "," and depth == 0:
                    parts.append(cur)
                    cur = ""
                else:
                    cur += ch
            parts.append(cur)
            if len(parts) < 4:
                offenders.append(f"{path.relative_to(root)}: {call}")
    assert not offenders, "was_processed called without user_id:\n" + "\n".join(offenders)
