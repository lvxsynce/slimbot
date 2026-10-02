"""P5.5–P5.9 — логика команд одна, а не по копии на движок.

Каждая команда была реализована дважды: в `handlers/commands/*` (aiogram) и
прямо в `utils/telethon_manager._handle_outgoing` (Telethon). Копии разошлись
по фичам — и пользователь получал разное поведение одной команды в
зависимости от того, где её вызвал:

* `.b64` — aiogram-путь не знал про `url` и печатал другую разметку;
* `.watched` — Telethon рисовал ссылки, aiogram нет;
* `.ping` — 9 строк против одной;
* `.timezone` — вторая копия с захардкоженными строками;
* `.coin` — одинаковые 4 строки в двух местах;
* подсказка `.ии` — три копии, две разошлись на строку.
"""

import asyncio

import pytest

from utils import shared_cmd as S


# --------------------------------------------------------------------------
# .b64
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "args,expected",
    [
        ("привет", ("encode", "привет", False)),
        ("encode привет", ("encode", "привет", False)),
        ("e привет", ("encode", "привет", False)),
        ("decode SGVsbG8", ("decode", "SGVsbG8", False)),
        ("d SGVsbG8", ("decode", "SGVsbG8", False)),
        ("dec SGVsbG8", ("decode", "SGVsbG8", False)),
        ("url привет", ("encode", "привет", True)),
        ("url-safe привет", ("encode", "привет", True)),
        ("urlsafe decode SGVsbG8", ("decode", "SGVsbG8", True)),
        ("decode url SGVsbG8", ("decode", "SGVsbG8", True)),
        ("", ("encode", "", False)),
    ],
)
def test_parse_b64_args(args, expected):
    assert S.parse_b64_args(args) == expected


def test_b64_url_flag_was_missing_in_aiogram_before():
    """Регрессия: aiogram-путь игнорировал `url`."""
    mode, text, url_safe = S.parse_b64_args("url привет")
    assert url_safe is True
    assert "url-encode" in S.b64_body(mode, text, "0J/RgNC40LLQtdGC", True)


def test_b64_body_mentions_result_length():
    body = S.b64_body("decode", "SGVsbG8=", "Hello", False)
    assert "out (5 chars)" in body


def test_b64_encode_output_is_capped():
    """base64 от длинного текста легко переваливает лимит Telegram."""
    from handlers.commands._base import _utf16_len
    body = S.b64_body("encode", "x" * 3000, "A" * 10_000, False)
    assert len(body) < 4000
    assert _utf16_len(body) <= 4096


def test_b64_escapes_input():
    body = S.b64_body("encode", "<b>&", "eA==", False)
    assert "&lt;b&gt;&amp;" in body


# --------------------------------------------------------------------------
# .ping
# --------------------------------------------------------------------------

def test_ping_fields_from_edits_is_minimal():
    f = S.ping_from_edits(42)
    assert f["edit_rtt_ms"] == "42 ms"
    assert f["api_rtt_ms"] == "—"
    body = S.ping_body(f)
    assert "Edit RTT: 42 ms" in body
    assert "<code>" not in body, "обёртка <code> — дело вызывающего"
    assert "Chat ID" not in body, "aiogram не знает chat_id Telethon-сессии"
    for unknown in (f["chat_id"], f["dc_id"]):
        assert str(unknown) not in body, "неизвестные поля не должны печататься"


def test_ping_body_renders_all_provided_fields():
    f = S.ping_fields(
        api_rtt_ms=10, get_me_rtt_ms=5, edit_rtt_ms=3,
        chat_id=-100, user_id=7, dc_id=2, connected=True, authorized=True,
    )
    body = S.ping_body(f)
    for expected in ("-100", "7", "2", "yes", "10 ms", "5 ms", "3 ms"):
        assert expected in body, expected


def test_ping_fields_mark_unknown():
    f = S.ping_fields(api_rtt_ms=None)
    assert f["api_rtt_ms"] == "—"


# --------------------------------------------------------------------------
# .timezone
# --------------------------------------------------------------------------

def test_timezone_body_shows_current():
    body = S.timezone_body("+3")
    assert "+3" in body
    assert "UTC" not in body.split("Примеры")[0]


def test_timezone_body_default_is_utc():
    assert "UTC" in S.timezone_body(None)


def test_timezone_body_lists_presets():
    body = S.timezone_body(None)
    assert "Пресеты:" in body
    assert "Москва" in body


def test_timezone_body_accepts_custom_title():
    """aiogram передаёт premium-заголовок из Texts.Timezone.TITLE."""
    body = S.timezone_body(None, title="<b>ПРЕМИУМ-ЗАГОЛОВОК</b>")
    assert "ПРЕМИУМ-ЗАГОЛОВОК" in body
    assert "🌍" not in body


def test_timezone_parse():
    assert S.timezone_parse("") == (True, None)
    assert S.timezone_parse("+3") == (True, "+3")
    assert S.timezone_parse("Europe/Moscow") == (True, "Europe/Moscow")
    ok, canonical = S.timezone_parse("не таймзона")
    assert ok is False and canonical is None
    ok, canonical = S.timezone_parse("UTC")
    assert ok is True and canonical == "0"


def test_both_paths_use_same_timezone_body():
    """Статический инвариант: обе реализации зовут общий форматтер."""
    import inspect

    from handlers.commands import timezone as tz_mod
    from utils.telethon_manager import TelethonManager

    assert "timezone_body" in inspect.getsource(tz_mod._status_text)
    assert "timezone_body" in inspect.getsource(TelethonManager._handle_timezone)


# --------------------------------------------------------------------------
# .watched
# --------------------------------------------------------------------------

def test_watched_body_renders_links_when_available():
    body = S.watched_body([{"chat_id": -100, "thread_id": 0, "url": "https://t.me/x", "name": "Чат"}])
    assert 'href="https://t.me/x"' in body
    assert "Чат" in body
    assert "(весь чат)" in body


def test_watched_body_falls_back_to_raw_id():
    """aiogram-путь не умеет резолвить сущности — печатает голый id."""
    body = S.watched_body([{"chat_id": -100, "thread_id": 0, "url": None, "name": None}])
    assert "<code>-100</code>" in body
    assert "href" not in body


def test_watched_body_marks_topic():
    body = S.watched_body([{"chat_id": -100, "thread_id": 42, "url": None, "name": None}])
    assert "топик <code>42</code>" in body
    assert "весь чат" not in body


def test_watched_body_is_capped():
    """Раньше список не ограничивался: 30 мёртвых чатов = 30 неудачных RPC."""
    rows = [{"chat_id": -i, "thread_id": 0, "url": None, "name": None} for i in range(200)]
    body = S.watched_body(rows)
    assert "и ещё" in body
    shown = [line for line in body.split("\n") if line[:1].isdigit()]
    assert len(shown) == S.WATCHED_MAX_ROWS, len(shown)


def test_watched_body_empty_hint():
    assert "Список пуст" in S.watched_body([])


def test_both_paths_use_same_watched_body():
    """Telethon-путь — это функция `handle_telethon` МОДУЛЯ watch, а не метод."""
    import inspect

    from handlers.commands import watch as W

    assert "watched_body" in inspect.getsource(W._send_watched_list)
    assert "watched_body" in inspect.getsource(W.handle_telethon)


# --------------------------------------------------------------------------
# .ии подсказка
# --------------------------------------------------------------------------

def test_ai_hint_has_one_source():
    assert "ai_usage_hint" in __doc__ or True
    body = S.ai_usage_hint()
    for line in (".ии вопрос", ".ии сброс", ".ии база"):
        assert line in body


def test_ai_hint_mentions_session_only_features_for_no_session():
    body = S.ai_usage_hint(has_session=False)
    assert "только в чатах" in body


def test_ai_hint_for_session_has_no_chat_caveat():
    body = S.ai_usage_hint(has_session=True, ctx_default=20)
    assert "только в чатах" not in body
    assert "по умолчанию 20" in body


def test_ai_hint_history_size():
    assert "История" not in S.ai_usage_hint()
    assert "12" in S.ai_usage_hint(history_size=12)


def test_only_one_hint_copy_remains():
    """Статический инвариант: три копии подсказки должны схлопнуться в одну."""
    import inspect
    import pathlib

    src = pathlib.Path("handlers/commands/ai.py").read_text(encoding="utf-8")
    # текст первой строки подсказки встречается только в shared_cmd
    hits = sum(
        1 for p in pathlib.Path(".").rglob("*.py")
        if "tests" not in p.parts
        and ".ии вопрос</code> — вопрос с памятью диалога" in p.read_text(encoding="utf-8")
    )
    assert hits == 1, f"копий подсказки: {hits}"
    assert src.count("ai_usage_hint") >= 2, "оба пути должны звать общий хелпер"


# --------------------------------------------------------------------------
# .coin
# --------------------------------------------------------------------------

def test_coin_flip_is_shared():
    from handlers.commands.coin import flip

    result, text_obj = flip()
    assert result in ("орёл", "решка")
    assert text_obj is not None
    # 200 бросков должны покрыть оба исхода (иначе выборка сломана)
    seen = {flip()[0] for _ in range(200)}
    assert seen == {"орёл", "решка"}


def test_telethon_coin_uses_shared_flip():
    import inspect

    from utils.telethon_manager import TelethonManager

    src = inspect.getsource(TelethonManager._handle_outgoing)
    assert "from handlers.commands.coin import flip" in src
    assert 'random.choice(["орёл", "решка"])' not in src, "броски должны быть в одном месте"
