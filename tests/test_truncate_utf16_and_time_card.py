"""P8.4/P8.5 — троттлинг по UTF-16 и отсутствие дублей в карточке `.time`.

- P8.4: Telegram считает длину в UTF-16 code units, а `len()` в Python даёт
  code points. Карточка с 3000 emoji = 6000 UTF-16 units проходила мимо
  проверки `len(text) <= 4096` и уходила в BadRequest.
- P8.5: `format_with_tz` печатал собственный заголовок «Текущее время»,
  а `Texts.Time.TIME` печатал его же — пользователь видел его дважды.
"""

import asyncio
import re

import pytest

from handlers.commands._base import _truncate, _utf16_len, render_time
from utils.timezones import format_with_tz


# --------------------------------------------------------------------------
# P8.4 — UTF-16
# --------------------------------------------------------------------------

def test_utf16_len_differs_from_len_for_emoji():
    """Смысл всей правки: len() не считает то, что считает Telegram."""
    s = "🕐" * 100
    assert len(s) == 100
    assert _utf16_len(s) == 200
    assert _utf16_len(s) != len(s)


def test_ascii_length_matches_len():
    assert _utf16_len("abc") == 3


@pytest.mark.parametrize(
    "text",
    [
        "🕐" * 3000,          # чистые emoji
        "A" * 10000,          # чистый ASCII
        "a🕐b" * 2000,        # смешанный
        "🕐" * 2000 + "</code>",
    ],
)
def test_truncate_respects_utf16_limit(text):
    out = _truncate(text)
    assert _utf16_len(out) <= 4096, f"utf16={_utf16_len(out)}"


def test_truncate_actually_shrinks_emoji_card():
    """Регрессия: 3000 emoji = 6000 UTF-16 проходили нетронутыми."""
    s = "🕐" * 3000
    assert _utf16_len(s) > 4096
    out = _truncate(s)
    assert len(out) < len(s), "emoji-heavy card must be truncated"
    assert _utf16_len(out) <= 4096


def test_truncate_leaves_short_text_untouched():
    assert _truncate("коротко") == "коротко"


def test_truncate_still_balances_tags():
    out = _truncate("<code>" + "🕐" * 3000)
    assert out.count("<code>") == out.count("</code>")


def test_command_card_end_to_end_utf16():
    from handlers.commands._base import command_card
    card = command_card("T", "🕐" * 3000)
    assert _utf16_len(card) <= 4096, _utf16_len(card)


# --------------------------------------------------------------------------
# P8.5 — заголовок печатается один раз
# --------------------------------------------------------------------------

def test_format_with_tz_has_no_heading():
    """Сама функция больше не печатает «Текущее время»."""
    text, _ = format_with_tz("+3")
    assert "Текущее время" not in text, text


def test_format_with_tz_has_no_blockquote():
    text, _ = format_with_tz("+3")
    assert "<blockquote" not in text


def test_format_with_tz_keeps_the_time():
    text, _ = format_with_tz("+3")
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text), text
    assert "UTC+3" in text


@pytest.mark.parametrize("uid", [None, 8002855167])
def test_render_time_heading_appears_once(uid):
    html = asyncio.run(render_time(uid))
    assert html.count("Текущее время") == 1, f"heading duplicated:\n{html}"


def test_render_time_has_exactly_one_blockquote():
    html = asyncio.run(render_time(None))
    assert html.count("<blockquote") == 1
    assert html.count("</blockquote>") == 1


def test_render_time_still_has_a_clock():
    html = asyncio.run(render_time(None))
    assert "⏰" in html, "clock emoji must remain"
