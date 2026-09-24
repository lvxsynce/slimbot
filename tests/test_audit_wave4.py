"""Тесты фиксов аудита, волна 4: AI-пайплайн."""

import asyncio

from handlers.commands.ai import (
    _parse_query,
    _is_reset_all,
    _strip_tool_calls,
    _format_ai_response,
    _collect_context_safe,
    _downscale_image,
)


def test_parse_query_ctx_first_wins_all_stripped():
    q, _, _, ctx = _parse_query(".ии ctx=10 ctx=50 вопрос")
    assert ctx == 10
    assert q == "вопрос"
    assert "ctx=" not in q


def test_parse_query_ctx_invalid_stripped_no_override():
    q, _, _, ctx = _parse_query(".ии ctx=abc вопрос")
    assert ctx is None
    assert q == "вопрос"


def test_parse_query_ctx_in_url_untouched():
    q, _, _, ctx = _parse_query(".ии что такое site.com?ctx=1")
    assert ctx is None
    assert "site.com?ctx=1" in q


def test_parse_query_ctx_clamped():
    _, _, _, ctx = _parse_query(".ии ctx=999 вопрос")
    assert ctx == 200
    _, _, _, ctx0 = _parse_query(".ии ctx=0 вопрос")
    assert ctx0 == 0


def test_is_reset_all():
    # Пунктуация снимается в _parse_query (STRIP_PUNCT) до вызова —
    # сюда приходит уже чистый текст.
    assert _is_reset_all("все") is True
    assert _is_reset_all("all") is True
    assert _is_reset_all("все лишнее") is True
    assert _is_reset_all("all chat") is True
    assert _is_reset_all("") is False
    assert _is_reset_all("всё") is False


def test_parse_query_reset_all_punctuation():
    q, is_reset, _, _ = _parse_query(".ии сброс все!")
    assert is_reset is True
    assert _is_reset_all(q) is True


def test_strip_orphan_keeps_following_lines():
    out = _strip_tool_calls("a <tool_use>oops\nb <tool_use>me</tool_use> c")
    assert "oops" not in out
    assert "b" in out and "c" in out
    assert "<tool_use>" not in out


def test_format_response_header_outside_quote():
    body = _format_ai_response(
        cleaned="привет", tool_log=[], is_debug=False,
        history_size=5, ctx_size=50, usage=None,
    )
    assert body.startswith("<b>Slim bot | AI</b>")
    assert body.count("<blockquote>") == 1
    assert "память: 5" in body
    assert "конт: ±50" in body


def test_format_response_debug_adds_second_block():
    body = _format_ai_response(
        cleaned="привет", tool_log=[("me", "", "ok", 0)],
        is_debug=True, history_size=None, ctx_size=None, usage=None,
    )
    assert body.count("<blockquote>") == 2
    assert "использовал: me" in body


def _mock_client(messages):
    class Client:
        async def iter_messages(self, chat_id, min_id=None, max_id=None, limit=None, reverse=False):
            out = [m for m in messages if (min_id is None or m.id > min_id) and (max_id is None or m.id < max_id)]
            out = out[:limit] if limit else out
            for m in out:
                yield m

    return Client()


def _mock_msg(mid, text="hi"):
    from types import SimpleNamespace

    return SimpleNamespace(id=mid, raw_text=text, message=text, sender_id=1)


def test_collect_context_is_symmetric():
    msgs = [_mock_msg(i, f"m{i}") for i in range(1, 21)]
    ctx = asyncio.run(_collect_context_safe(_mock_client(msgs), -100, 10, 2, thread_id=0))
    texts = [c["content"] for c in ctx]
    joined = "\n".join(texts)
    for want in ("m8", "m9", "m11", "m12"):
        assert want in joined, want
    assert "m10" not in joined


def test_collect_context_zero_is_empty():
    msgs = [_mock_msg(i) for i in range(1, 21)]
    assert asyncio.run(_collect_context_safe(_mock_client(msgs), -100, 10, 0, thread_id=0)) == []


def test_downscale_large_image():
    try:
        from PIL import Image
    except ImportError:
        return
    import io as _io

    im = Image.new("RGB", (3000, 2000), "red")
    buf = _io.BytesIO()
    im.save(buf, "PNG")
    data, mime = _downscale_image(buf.getvalue())
    assert mime == "image/jpeg"
    small = Image.open(_io.BytesIO(data))
    assert max(small.size) <= 1280


def test_downscale_rejects_animated():
    try:
        from PIL import Image
    except ImportError:
        return
    import io as _io

    frames = [Image.new("RGB", (64, 64), c) for c in ("red", "blue")]
    buf = _io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], loop=0)
    assert _downscale_image(buf.getvalue()) == (None, None)
