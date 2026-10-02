"""Inline-режим: маршрутизация, подсказки, авторизация и экранирование.

У inline не было ни одного теста, а ломается он двумя способами:

1. **Двойной ответ.** aiogram прогоняет всех observers, чьи фильтры
   прошли, поэтому два `@router.inline_query()` дают два
   `inline.answer()` → `BadRequest`. Отсюда жёсткое требование: ровно
   одна точка входа.
2. **Неэкранированный ввод.** `InlineQuery.query` и поля профиля
   попадают в HTML-карточку; без `esc()` юзер может сломать разметку
   (и попытаться внедрить что-то вроде `<a href=...>`).

`InlineQuery` — frozen pydantic, поэтому диспетчер проверяется на
минимальном стабе, который считает вызовы `answer()`.
"""

import asyncio
import re
from types import SimpleNamespace

import pytest
from telethon.tl.types import User as TUser

import handlers.inline as I
from handlers.inline import dispatch_inline, help as help_mod, profile as profile_mod, status as status_mod
from utils import inline_kb
from utils.inline_kb import connect_button, switch_inline_markup, with_inline_hint

NO_SESSION_UID = 424242424242  # заведомо без сессии
WITH_SESSION_UID = 515151515151


# --------------------------------------------------------------------------
# Стабы
# --------------------------------------------------------------------------

class FakeInlineQuery:
    """Считает ответы: >1 = тот самый BadRequest из-за двух хендлеров."""

    def __init__(self, query: str = "", uid: int = NO_SESSION_UID):
        self.query = query
        self.from_user = SimpleNamespace(id=uid)
        self.answers: list[dict] = []

    async def answer(self, results=None, **kwargs):
        self.answers.append({"results": list(results or []), **kwargs})
        return True

    @property
    def answer_count(self) -> int:
        return len(self.answers)

    @property
    def result_ids(self) -> list[str]:
        return [r.id for r in self.answers[0]["results"]]

    @property
    def text(self) -> str:
        return self.answers[0]["results"][0].input_message_content.message_text


class FakeBot:
    """`getMe` для расчёта пинга."""

    def __init__(self, ping_ms: int | None = 12):
        self._ping_ms = ping_ms

    async def get_me(self):
        if self._ping_ms is None:
            raise RuntimeError("network down")
        return SimpleNamespace(username="slimsec_robot", id=1)


class FakeClient:
    """Telethon-клиент, отдающий заранее заданные entity по target'у."""

    def __init__(self, mapping: dict | None = None, error_for=()):
        self._mapping = mapping or {}
        self._error_for = set(error_for)
        self.calls: list[str] = []

    async def get_entity(self, target):
        self.calls.append(target)
        if target in self._error_for:
            raise ValueError("boom")
        return self._mapping.get(target)


def _user(uid: int, **kw) -> TUser:
    return TUser(id=uid, first_name=kw.pop("first_name", "U"), **kw)


@pytest.fixture
def no_session(monkeypatch):
    """Ни у кого нет Telethon-сессии."""
    for mod in (profile_mod, help_mod, status_mod):
        monkeypatch.setattr(mod, "session_exists", lambda uid: False)


@pytest.fixture
def with_session(monkeypatch):
    for mod in (profile_mod, help_mod, status_mod):
        monkeypatch.setattr(mod, "session_exists", lambda uid: True)


def _dispatch(query: str, uid: int = NO_SESSION_UID) -> FakeInlineQuery:
    inline = FakeInlineQuery(query, uid)
    asyncio.run(dispatch_inline(inline, FakeBot()))
    return inline


# --------------------------------------------------------------------------
# 1. Единственная точка входа
# --------------------------------------------------------------------------

def test_inline_router_has_exactly_one_handler():
    assert len(I.router.inline_query.handlers) == 1, "у inline-роутера один хендлер"


def test_no_submodule_registers_its_own_inline_query():
    """Сабмодули отдают чистые функции; свой @router.inline_query = двойной ответ."""
    for mod in (help_mod, status_mod, profile_mod):
        routers = [
            obj for obj in vars(mod).values()
            if hasattr(obj, "inline_query") and hasattr(obj, "sub_routers")
        ]
        assert not routers, f"{mod.__name__} регистрирует свой inline-роутер"


@pytest.mark.parametrize("query", [
    "", " ", "помощь", "help", "справка", "h", "?",
    "статус", "status",
    "@durov", "@durov @telegram", "@", "@help", "@статус", "@?",
    "абракадабра", ".ping", "что это",
])
def test_dispatch_answers_exactly_once(query, no_session):
    """ГЛАВНАЯ проверка: на любой запрос ровно один inline.answer."""
    inline = _dispatch(query)
    assert inline.answer_count == 1, f"{query!r} → {inline.answer_count} ответов"


# --------------------------------------------------------------------------
# 2. Маршрутизация
# --------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_id", [
    ("", "help_unauthorized"),
    ("помощь", "help_unauthorized"),
    ("help", "help_unauthorized"),
    ("справка", "help_unauthorized"),
    ("h", "help_unauthorized"),
    ("?", "help_unauthorized"),
    ("статус", "status_locked"),
    ("status", "status_locked"),
    ("@", "profile_hint"),
    ("@help", "help_unauthorized"),      # команда, а не username
    ("@статус", "status_locked"),
    ("@h", "help_unauthorized"),
    ("@durov", "profile_need_session"),
    ("бред", "help_unknown_query"),
])
def test_routing_without_session(query, expected_id, no_session):
    assert _dispatch(query).result_ids == [expected_id]


def test_help_and_status_authorized_articles(with_session):
    assert _dispatch("").result_ids == ["help_main"]
    assert _dispatch("статус").result_ids == ["status_main"]


def test_at_prefixed_keyword_beats_profile(with_session):
    """`@help` обязан уйти в справку, а не в резолв юзернейма `help`."""
    assert _dispatch("@help", WITH_SESSION_UID).result_ids == ["help_main"]
    assert _dispatch("@status", WITH_SESSION_UID).result_ids == ["status_main"]


# --------------------------------------------------------------------------
# 3. Подсказка на нераспознанный запрос (было: results=[])
# --------------------------------------------------------------------------

def test_unknown_query_is_not_silent(no_session):
    """Раньше на неизвестный запрос был `results=[]` — юзер не видел ничего."""
    inline = _dispatch("что-то непонятное")
    assert inline.result_ids == ["help_unknown_query"]
    assert "статус" in inline.text, "подсказка должна перечислять команды"


def test_hint_escapes_user_input(no_session):
    inline = _dispatch("<b>жирный</b> & <script>")
    assert "<b>жирный</b>" not in inline.text
    assert "&lt;b&gt;жирный&lt;/b&gt;" in inline.text
    assert "<script>" not in inline.text


def test_hint_echoes_the_query(no_session):
    inline = _dispatch("абракадабра")
    assert "абракадабра" in inline.text


def test_hint_has_connect_button(no_session):
    """Юзер без сессии должен получить путь к подключению прямо из подсказки."""
    assert _dispatch("что-то").answers[0]["button"].start_parameter == "start"


# --------------------------------------------------------------------------
# 4. Авторизация и наличие клиента
# --------------------------------------------------------------------------

def test_profile_without_client_is_empty(monkeypatch, with_session):
    monkeypatch.setattr(profile_mod.telethon_manager, "get_client", lambda uid: None)
    inline = _dispatch("@durov", WITH_SESSION_UID)
    assert inline.result_ids == []
    assert inline.answer_count == 1


def test_profile_resolves_via_client(monkeypatch, with_session):
    client = FakeClient({"durov": _user(1, first_name="Павел", username="durov")})
    monkeypatch.setattr(profile_mod.telethon_manager, "get_client", lambda uid: client)
    inline = _dispatch("@durov", WITH_SESSION_UID)
    assert client.calls == ["durov"], "@-префикс должен быть срезан до резолва"
    assert inline.result_ids == ["profile_1"]
    assert "Павел" in inline.text


def test_profile_multiple_targets(monkeypatch, with_session):
    client = FakeClient({
        "a": _user(1, first_name="A"),
        "b": _user(2, first_name="B"),
    })
    monkeypatch.setattr(profile_mod.telethon_manager, "get_client", lambda uid: client)
    inline = _dispatch("@a @b", WITH_SESSION_UID)
    assert inline.result_ids == ["profile_1", "profile_2"]


def test_profile_error_target_becomes_article(monkeypatch, with_session):
    client = FakeClient({"a": _user(1, first_name="A")}, error_for={"nope"})
    monkeypatch.setattr(profile_mod.telethon_manager, "get_client", lambda uid: client)
    inline = _dispatch("@a @nope", WITH_SESSION_UID)
    results = inline.answers[0]["results"]
    assert [r.id for r in results] == ["profile_1", "profile_err_nope"]
    assert results[1].title == "Ошибка: nope"
    assert "boom" in results[1].input_message_content.message_text


def test_unknown_entity_becomes_error_article(monkeypatch, with_session):
    """`get_entity` вернул None без исключения — раньше был AttributeError
    прямо в хендлере (юзер видел бы пустой список)."""
    monkeypatch.setattr(
        profile_mod.telethon_manager, "get_client",
        lambda uid: FakeClient({"ghost": None}),
    )
    inline = _dispatch("@ghost", WITH_SESSION_UID)
    assert inline.result_ids == ["profile_err_ghost"]
    assert "сущность не найдена" in inline.text


def test_same_entity_twice_gives_unique_ids(monkeypatch, with_session):
    """Дубль id в одном ответе роняет весь ответ BadRequest'ом."""
    same = _user(7, first_name="Дубль")
    client = FakeClient({"durov": same, "ДУРОВ": same})
    monkeypatch.setattr(profile_mod.telethon_manager, "get_client", lambda uid: client)
    inline = _dispatch("@durov @ДУРОВ", WITH_SESSION_UID)
    ids = inline.result_ids
    assert len(ids) == 2
    assert len(set(ids)) == 2, ids
    assert all(len(i) <= 64 for i in ids)


def test_long_target_name_keeps_id_within_limit(monkeypatch, with_session):
    long_id = 123456789
    client = FakeClient({"x" * 200: _user(long_id, first_name="Длинный")})
    monkeypatch.setattr(profile_mod.telethon_manager, "get_client", lambda uid: client)
    inline = _dispatch("@" + "x" * 200, WITH_SESSION_UID)
    assert all(len(i) <= 64 for i in inline.result_ids)


# --------------------------------------------------------------------------
# 5. Парсинг целей
# --------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("", []),
    ("   ", []),
    ("@", []),
    ("@durov", ["durov"]),
    ("durov", ["durov"]),
    ("@a @b", ["a", "b"]),
    ("@a,@b b", ["a", "b"]),
    ("@a @A", ["a"]),            # дедуп case-insensitive, порядок ввода
    ("@B @a", ["B", "a"]),        # регистр первого ввода сохраняется
    ("-1001234567", ["-1001234567"]),
])
def test_parse_inline_targets(query, expected):
    assert profile_mod.parse_inline_targets(query) == expected


def test_parse_inline_targets_respects_limit():
    assert profile_mod.parse_inline_targets("@a @b @c", limit=2) == ["a", "b"]


def test_parse_inline_targets_uses_configured_limit():
    from config import DOT_TARGET_LIMIT

    assert len(profile_mod.parse_inline_targets(" ".join(f"@u{i}" for i in range(99)))) \
        == DOT_TARGET_LIMIT


@pytest.mark.parametrize("query,expected", [
    ("@durov", True),
    (" @durov", True),
    ("", False),
    ("статус", False),
])
def test_is_profile_query(query, expected):
    assert profile_mod.is_profile_query(query) is expected


# --------------------------------------------------------------------------
# 6. Карточки: экранирование и лимиты
# --------------------------------------------------------------------------

def test_profile_card_escapes_all_user_fields():
    entity = _user(
        5,
        first_name="<b>Имя</b>",
        last_name="&amp;",
        username='x" onload="1',
        phone="+7<b>999</b>",
        lang_code="ru<script>",
    )
    html = profile_mod._format_profile(entity, None)
    assert "<b>Имя</b>" not in html, "first_name не экранирован"
    assert "&lt;b&gt;Имя&lt;/b&gt;" in html
    assert "<script>" not in html
    assert "+7&lt;b&gt;999&lt;/b&gt;" in html, "телефон не экранирован"
    assert html.count("<blockquote>") == 1


def test_profile_card_truncates_about():
    entity = _user(6, first_name="A")
    full = SimpleNamespace(about="x" * 500, common_chats_count=7)
    html = profile_mod._format_profile(entity, full)
    assert "…" in html
    assert "x" * 500 not in html
    assert "Общие чаты: 7" in html


def test_profile_card_shows_flags():
    entity = _user(8, first_name="A", premium=True, verified=True, scam=True, bot=True)
    html = profile_mod._format_profile(entity, None)
    assert "Premium: ⭐ да" in html
    assert "✓ verified" in html
    assert "⚠ scam" in html
    assert "🤖 Бот" in html


def test_status_card_contents(with_session):
    text = asyncio.run(status_mod._build_status_text(FakeBot()))
    assert "<blockquote>" in text and text.count("<blockquote>") == 1
    assert re.search(r"Пинг до Telegram \(getMe\): <code>\d+ms</code>", text)
    assert "Аптайм" in text


def test_status_card_survives_broken_api(with_session):
    text = asyncio.run(status_mod._build_status_text(FakeBot(ping_ms=None)))
    assert "<code>—</code>" in text, "упавший getMe не должен ронять карточку"


@pytest.mark.parametrize("seconds,expected", [
    (0, "—"), (-5, "—"), (30, "30 сек"), (90, "1м 30с"),
    (3600, "1ч 0м"), (90_000, "1д 1ч"),
])
def test_fmt_uptime(seconds, expected):
    assert status_mod._fmt_uptime(seconds) == expected


def test_help_text_mentions_all_inline_commands(with_session):
    text = _dispatch("").text
    assert "статус" in text and "@username" in text
    assert "<blockquote>" not in text, "инлайн-справка — не command_card"


# --------------------------------------------------------------------------
# 7. Кеши приезжают из config (магических чисел быть не должно)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mod", [help_mod, status_mod, profile_mod, I])
def test_no_magic_cache_numbers(mod, with_session):
    """Все cache_time — из config; литерал означает «забыли вынести в config»."""
    import inspect

    src = inspect.getsource(mod)
    assert not re.search(r"cache_time\s*=\s*\d", src), (
        f"{mod.__name__}: cache_time-литерал вместо config.INLINE_CACHE_*"
    )


def test_cache_config_values_are_used(with_session, monkeypatch):
    """Правка config.INLINE_CACHE_STATUS обязана влиять на ответ."""
    import config

    seen: dict = {}

    async def spy(self, results=None, **kwargs):
        seen.update(kwargs)

    inline = FakeInlineQuery("статус", WITH_SESSION_UID)
    monkeypatch.setattr(FakeInlineQuery, "answer", spy)
    asyncio.run(dispatch_inline(inline, FakeBot()))
    assert seen["cache_time"] == config.INLINE_CACHE_STATUS


# --------------------------------------------------------------------------
# 8. Подсказка-кнопка (единственный механизм placeholder в Bot API)
# --------------------------------------------------------------------------

def test_switch_inline_markup_builds_chosen_chat():
    kb = switch_inline_markup()
    btn = kb.inline_keyboard[0][0]
    sw = btn.switch_inline_query_chosen_chat
    assert sw is not None, "нужен switch_inline_query_chosen_chat, не current_chat"
    assert btn.callback_data is None
    assert sw.allow_user_chats and sw.allow_group_chats and sw.allow_channel_chats
    assert sw.query is None


def test_switch_inline_markup_keeps_query():
    sw = switch_inline_markup("статус").inline_keyboard[0][0].switch_inline_query_chosen_chat
    assert sw.query == "статус"


def test_with_inline_hint_appends_and_keeps_buttons():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    base = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="c1", callback_data="c1")],
    ])
    kb = with_inline_hint(base)
    assert len(kb.inline_keyboard) == 2
    assert kb.inline_keyboard[0][0].callback_data == "c1"
    assert kb.inline_keyboard[1][0].switch_inline_query_chosen_chat is not None


def test_connect_button_uses_deep_link():
    btn = connect_button()
    assert btn.start_parameter == inline_kb.START_PARAMETER == "start"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", btn.start_parameter)


def test_start_keyboard_offers_inline_hint():
    """Подсказка обязана быть в /start — иначе о ней никто не узнает."""
    from handlers.commands import start as start_mod

    for kwargs in ({"has_session": True}, {"has_session": False},
                   {"has_session": False, "may_connect": False}):
        kb = start_mod._session_kb(**kwargs)
        switchers = [
            b for row in kb.inline_keyboard for b in row
            if b.switch_inline_query_chosen_chat
        ]
        assert len(switchers) == 1, kwargs
        assert switchers[0] is kb.inline_keyboard[-1][0], "подсказка — последней строкой"


def test_help_keyboard_offers_inline_hint():
    from handlers.commands.help import help_keyboard

    for has_session in (True, False):
        kb = help_keyboard(has_session)
        assert any(
            b.switch_inline_query_chosen_chat for row in kb.inline_keyboard for b in row
        ), has_session


def test_help_text_lists_inline_commands():
    from handlers.commands._base import _help_inline_lines, _help_lines

    lines = _help_inline_lines()
    assert len(lines) == 3
    assert any("@username" in ln for ln in lines)
    assert any("Inline" in ln for ln in _help_lines(False))


# --------------------------------------------------------------------------
# 9. Свойства карточек, которые Telegram проверяет жёстко
# --------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "", "помощь", "статус", "@durov", "@", "@help", "@a @b", "бред", "x" * 500,
])
def test_result_ids_are_unique_and_short(query, with_session, monkeypatch):
    monkeypatch.setattr(
        profile_mod.telethon_manager, "get_client",
        lambda uid: FakeClient({"a": _user(1), "b": _user(2)}),
    )
    inline = _dispatch(query, WITH_SESSION_UID)
    ids = inline.result_ids
    assert len(set(ids)) == len(ids), f"{query!r}: дубли id {ids}"
    assert all(1 <= len(i) <= 64 for i in ids)


@pytest.mark.parametrize("query", ["", "помощь", "статус", "@durov", "бред"])
def test_titles_and_descriptions_are_not_empty(query, with_session, monkeypatch):
    monkeypatch.setattr(
        profile_mod.telethon_manager, "get_client", lambda uid: FakeClient({}),
    )
    for res in _dispatch(query, WITH_SESSION_UID).answers[0]["results"]:
        assert res.title, res.id
        assert len(res.title) <= 256, res.id
        assert len(res.id) <= 64, res.id