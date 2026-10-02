"""P1.3 — карточки не должны содержать вложенных `<blockquote>`.

Telegram отклоняет вложенные сущности одного типа (`Can't nest entities of
the same type`). Баг был в `.time`: `format_with_tz` возвращал готовую карточку
в `<blockquote>`, а `Texts.Time.TIME` оборачивал её ещё раз.
"""

import asyncio
import re

import pytest

from handlers.commands._base import command_card, render_id, render_time
from utils.texts import Texts
from utils.timezones import format_with_tz


def _assert_no_nested_blockquote(html: str, label: str):
    assert html.count("<blockquote") <= 1, f"{label}: too many blockquotes\n{html}"
    if "<blockquote" in html:
        assert html.count("</blockquote>") == 1, f"{label}: unbalanced\n{html}"
    # структурная проверка: ни одна открытая не вложена в другую
    depth = 0
    for match in re.finditer(r"<blockquote[^>]*>|</blockquote>", html):
        if match.group(0).startswith("</"):
            depth -= 1
        else:
            depth += 1
        assert depth <= 1, f"{label}: nested blockquote at offset {match.start()}\n{html}"
    assert depth == 0, f"{label}: unbalanced blockquote\n{html}"


def test_format_with_tz_returns_bare_text():
    """format_with_tz больше не оборачивает — карточку собирает render_time."""
    text, tz_obj = format_with_tz("+3")
    assert "<blockquote" not in text, text
    assert "</blockquote>" not in text, text
    assert "UTC+3" in text
    assert tz_obj is not None


def test_time_template_has_no_blockquote():
    """Texts.Time.TIME не должен оборачивать — иначе снова вложенность."""
    rendered = Texts.Time.TIME.render(premium=False, now="X")
    assert "<blockquote" not in rendered
    assert "Текущее время" in rendered


@pytest.mark.parametrize("uid", [None, 8002855167, 12345])
def test_render_time_has_single_blockquote(uid):
    html = asyncio.run(render_time(uid))
    _assert_no_nested_blockquote(html, f"render_time(uid={uid})")
    assert html.startswith("<b>Slim bot | Time</b>")
    assert "Текущее время" in html


def test_render_time_uses_users_timezone():
    from utils.storage import set_user_tz, user_timezones

    set_user_tz("555", "Europe/Moscow")
    try:
        html = asyncio.run(render_time("555"))
        assert "Europe/Moscow" in html, html
    finally:
        user_timezones.pop("555", None)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(chat_id=-100123, user_id=42, username="u", first_name="N"),
        dict(chat_id=-100123, user_id=42, username=None, first_name="", thread_id=7),
    ],
)
def test_render_id_has_single_blockquote(kwargs):
    html = asyncio.run(render_id(None, **kwargs))
    _assert_no_nested_blockquote(html, "render_id")


def test_command_card_is_balanced_on_repeated_wrap():
    """command_card не должен ломать уже готовую карточку."""
    inner = "line1\nline2"
    once = command_card("T", inner)
    twice = command_card("T", once)
    for html, label in ((once, "once"), (twice, "twice")):
        _assert_no_nested_blockquote(html, label)


def test_command_card_strips_legacy_header():
    legacy = "<b>Slim bot | Old</b>\n<blockquote>body</blockquote>"
    html = command_card("New", legacy)
    assert html.count("Slim bot |") == 1, html
    assert "Old" not in html
    _assert_no_nested_blockquote(html, "legacy-strip")
