"""Визуальное единство: help, w1, view-once caption, мёртвые тексты."""

import asyncio
from types import SimpleNamespace

from handlers.commands._base import format_help, render_help
from utils.telethon_manager import _view_once_caption
from utils.texts import Texts


def test_help_unified_both_states():
    no_ss = format_help(False)
    with_ss = format_help(True)
    for text in (no_ss, with_ss):
        assert ".del" in text
        assert ".watch @username" in text
        assert "С сессией ещё" not in text
        assert text.startswith("<b>Slim bot | Help</b>")
    assert "нужна сессия" in no_ss
    assert "/logout" in with_ss
    assert "/logout" not in no_ss


def test_render_help_matches_format_help():
    out = asyncio.run(render_help("nobody-no-session", False))
    assert ".del" in out
    assert "С сессией ещё" not in out


def test_view_once_caption_is_card():
    cap = _view_once_caption("фото", "Чат", "", -100, 0)
    assert cap.startswith("<b>Slim bot | View-once</b>")
    assert cap.count("<blockquote>") == 1
    assert "Одноразовое фото сохранено" in cap
    assert "<code>chat_id: -100</code>" in cap


def _stub_callback(uid):
    msg = SimpleNamespace(edits=[], kb=[])

    async def edit_text(text, reply_markup=None):
        msg.edits.append(text)
        msg.kb.append(reply_markup)

    async def answer(*args, **kwargs):
        pass

    msg.edit_text = edit_text
    return SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        message=msg,
        answer=answer,
    )


def test_why_no_session_offers_enable():
    from handlers.session import why_cb, WHY_TEXT

    cb = _stub_callback(424243)
    asyncio.run(why_cb(cb))
    assert cb.message.edits[0] == WHY_TEXT
    btns = [b.text for row in cb.message.kb[0].inline_keyboard for b in row]
    assert "[+] Включить" in btns


def test_why_with_session_offers_disable(monkeypatch):
    import utils.storage
    from handlers.session import why_cb, WHY_ACTIVE

    monkeypatch.setattr(utils.storage, "session_exists", lambda uid: True)
    cb = _stub_callback(424244)
    asyncio.run(why_cb(cb))
    assert cb.message.edits[0] == WHY_ACTIVE
    btns = [b.text for row in cb.message.kb[0].inline_keyboard for b in row]
    assert "[+] Включить" not in btns
    assert "[x] Выключить" in btns


def test_dead_texts_removed():
    assert not hasattr(Texts.Help, "TIP")
    assert not hasattr(Texts.Help, "WITH_SESSION_HEADER")
    assert not hasattr(Texts.Help, "NO_SESSION_HINT")
    assert not hasattr(Texts.Help, "DISABLE_HINT")
    assert not hasattr(Texts.Start, "DISCONNECTED_GREETING")
