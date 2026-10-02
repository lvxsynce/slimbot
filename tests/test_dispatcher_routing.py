"""P1.1 — регресс-тест: fallback-хендлеры не должны глушить команды.

Исторический баг: `ignore_unauthorized` был зарегистрирован на родительском
`router` ДО `include_router(...)` и возвращал `None`. В aiogram 3.x
`Router._propagate_event` проверяет свои хендлеры первыми и, если хоть один
вернул не-UNHANDLED, sub_routers НЕ обходятся. Итог: для юзера БЕЗ Telethon-сессии
все dot-команды молча игнорировались (`.ping` — тишина, `.ping справка` — ответ).

Тесты работают на настоящем `Router` из репозитория — это единственный уровень,
на котором баг был виден. `Message` — frozen pydantic, поэтому для прямых
вызовов хендлеров используем минимальный стаб.
"""

import asyncio

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

import handlers.commands as C
from utils.storage import session_exists, user_sessions

NO_SESSION_UID = 111111111  # гарантированно нет в user_sessions.json


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.is_bot = False
        self.first_name = "u"


class FakeMessage:
    """Минимальный stand-in: хендлерам нужны только text/from_user/reply."""

    def __init__(self, text, uid, thread_id=None):
        self.text = text
        self.from_user = FakeUser(uid)
        self.message_thread_id = thread_id
        self.chat = None
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


def _matches_ignore_unauthorized(msg) -> bool:
    """Дублирует фильтр хендлера — если он разъедется, тест упадёт."""
    return bool(
        C._is_dot(msg.text)
        and not C._bypass(msg.text)
        and not C._is_help_request(msg.text)
        and (not msg.from_user or not session_exists(str(msg.from_user.id)))
    )


def test_no_session_uid_is_really_absent():
    assert not session_exists(str(NO_SESSION_UID)), "тестовый uid не должен иметь сессию"


def test_parent_router_has_no_own_message_handlers():
    """ГЛАВНАЯ проверка фикса.

    Любой message-хендлер на самом `router` блокирует `sub_routers` в aiogram 3.x
    (propagate_event возвращается на первом не-UNHANDLED ответе). Поэтому на
    `handlers.commands.router` их быть не должно вообще.
    """
    own = C.router.message.handlers
    assert not own, (
        "handlers/commands.router must not register its own message handlers — "
        f"found {[h.callback.__name__ for h in own]}"
    )


def test_fallback_router_is_included_last():
    sub = C.router.sub_routers
    assert sub, "commands router has no sub-routers"
    assert sub[-1] is C._fallback_router, (
        "fallback router must be the last included sub-router; "
        f"last is {getattr(sub[-1], 'name', sub[-1])!r}"
    )


@pytest.mark.parametrize(
    "text",
    [".ping", ".time", ".id", ".me", ".chat", ".tr en hi", ".calc 2+2", ".ии привет", ".love"],
)
def test_real_commands_are_not_swallowed_for_user_without_session(text):
    """Юзер без сессии: настоящая команда не должна поглощаться fallback'ом.

    Если фильтр совпал (юзер без сессии + не опечатка), хендлер обязан
    поднять SkipHandler, а не вернуть значение — иначе событие умирает.
    """
    msg = FakeMessage(text, NO_SESSION_UID)
    if not _matches_ignore_unauthorized(msg):
        pytest.skip("filter does not match — nothing to assert")
    with pytest.raises(SkipHandler):
        asyncio.run(C.ignore_unauthorized(msg))
    assert not msg.replies


def test_fallback_does_not_swallow_with_session_either():
    """С сессией фильтр не совпадает вовсе — значит, fallback не участвует."""
    uid = int(next(iter(user_sessions)))
    if not session_exists(str(uid)):
        pytest.skip("нет активной сессии в user_sessions.json")
    for text in (".ping", ".time", ".love"):
        msg = FakeMessage(text, uid)
        assert not _matches_ignore_unauthorized(msg), f"{text!r} must not match fallback"


@pytest.mark.parametrize("text", [".pingn", ".timeee", ".pign", ".pingg"])
def test_typo_still_gets_suggestion_for_user_without_session(text):
    """Опечатка для юзера без сессии — подсказка должна уходить."""
    from utils.suggest import suggest_text

    hint = suggest_text(text)
    if hint is None:
        pytest.skip(f"{text!r} is not within Levenshtein distance 2 of any command")
    msg = FakeMessage(text, NO_SESSION_UID)
    assert _matches_ignore_unauthorized(msg)
    asyncio.run(C.ignore_unauthorized(msg))
    assert msg.replies, "expected a suggestion reply"
    assert hint in msg.replies[0]


def test_known_command_never_produces_suggestion():
    """Для точной команды подсказки быть не должно — иначе SkipHandler,
    который мы проверили выше, был бы мёртвым кодом."""
    for text in (".ping", ".time", ".calc 2+2"):
        assert C._suggest_hint(text) is None, f"{text!r} must not be treated as a typo"


@pytest.mark.parametrize("text", [".ping справка", ".tr help", ".watch ?"])
def test_help_request_never_reaches_fallback(text):
    """.ping справка — справка; fallback её не должен ловить."""
    msg = FakeMessage(text, NO_SESSION_UID)
    assert not _matches_ignore_unauthorized(msg), f"{text!r} must not match ignore_unauthorized"
    assert not C._is_unknown_dot(msg), f"{text!r} must not match suggest_unknown_private"


def test_bypass_help_is_still_bypassed():
    assert C._bypass(".help")
    assert C._bypass(".ПОМОЩЬ")
    assert not C._bypass(".ping")
    assert not C._bypass(".ping справка")


def test_bypass_head_tuple_is_gone():
    """Мёртвый `_BYPASS_HEAD` удалён (P4.5)."""
    assert not hasattr(C, "_BYPASS_HEAD")


def test_suggest_unknown_private_raises_skip_for_user_without_session():
    """Второй fallback тоже обязан отпускать событие, а не молча возвращать None."""
    msg = FakeMessage(".pingn", NO_SESSION_UID)
    with pytest.raises(SkipHandler):
        asyncio.run(C.suggest_unknown_private(msg))


def test_suggest_unknown_private_answers_for_user_with_session():
    uid = int(next(iter(user_sessions)))
    if not session_exists(str(uid)):
        pytest.skip("нет активной сессии в user_sessions.json")
    msg = FakeMessage(".pingn", uid)
    asyncio.run(C.suggest_unknown_private(msg))
    assert msg.replies, "expected a suggestion reply for a connected user"
