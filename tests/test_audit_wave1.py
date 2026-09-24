"""Тесты фиксов аудита, волна 1: Emoji.render, opencode, cmdhelp, extra."""

import asyncio
from types import SimpleNamespace

from utils.texts import Emoji, Text, Texts
from handlers.commands import opencode


def test_emoji_renders_fallback_without_premium():
    assert Emoji(fallback="✅").render(premium=False) == "✅"
    assert Emoji(fallback="✅").render(premium=True) == "✅"


def test_emoji_renders_tg_emoji_with_premium_id():
    e = Emoji(fallback="✅", premium_id="123")
    assert e.render(premium=True) == '<tg-emoji emoji-id="123">✅</tg-emoji>'
    assert e.render(premium=False) == "✅"


def test_text_render_keeps_literal_emoji_and_substitutes_placeholder():
    t = Text(template="<b>{clock} Hi ✅</b>", emojis={"clock": Emoji(fallback="⏰")})
    out = t.render(premium=False)
    assert "⏰" in out
    assert "✅" in out
    assert "{clock}" not in out


def test_ping_template_renders_with_ms():
    out = Texts.Ping.PING.render(premium=False, ms="12")
    assert "12ms" in out
    assert out.startswith("<b>Slim bot | Ping</b>")


def test_opencode_num_coerces_garbage():
    assert opencode._num(5) == 5
    assert opencode._num("42") == 42
    assert opencode._num("abc") == 0
    assert opencode._num(None) == 0
    assert opencode._num(3.9) == 3


def test_opencode_format_escapes_model_names():
    data = {
        "data": {
            "opencode_tokens": {
                "available": True,
                "today": {
                    "messages": 1, "input": 2, "output": 3,
                    "cache_read": 4, "total": 5,
                    "by_model": {"<b>evil</b>": {"input": "x", "output": 1}},
                },
                "week": {},
            }
        }
    }
    out = opencode.format_opencode_stats(data)
    assert "<b>evil</b>" not in out
    assert "&lt;b&gt;evil&lt;/b&gt;" in out


def test_opencode_fetch_without_pass_does_no_network(monkeypatch):
    monkeypatch.setattr(opencode, "OPENCODE_API_PASS", "")
    assert asyncio.run(opencode.fetch_opencode_stats()) is None


def _stub_message(text: str):
    calls = {}

    async def reply(reply_text, **kwargs):
        calls["text"] = reply_text
        calls["kwargs"] = kwargs

    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=999999),
        message_thread_id=None,
        reply=reply,
    ), calls


def test_cmdhelp_answers_without_session():
    from handlers.commands.cmdhelp import cmd_help_private

    msg, calls = _stub_message(".ping справка")
    asyncio.run(cmd_help_private(msg))
    assert "Ping" in calls["text"] or ".ping" in calls["text"]


def test_extra_me_renders_for_stub_user():
    from handlers.commands.extra import _make_me

    user = SimpleNamespace(
        id=123, first_name="Иван", last_name=None, username="ivan",
        usernames=None, language_code="ru", is_premium=False,
    )
    out = asyncio.run(_make_me("123", user))
    assert "<code>123</code>" in out
    assert "Иван" in out
