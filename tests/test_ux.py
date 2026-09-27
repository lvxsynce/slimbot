"""UX: status card, first-watch hint, help sections."""

import asyncio
from types import SimpleNamespace

from handlers.commands.status import _fmt_uptime, _photo_line, _status_lines
from handlers.commands.watch import _first_watch_hint
from handlers.commands.help import _section_lines, _render_section, help_keyboard
from utils import storage


def test_fmt_uptime():
    assert _fmt_uptime(0) == "—"
    assert _fmt_uptime(45) == "45 сек"
    assert _fmt_uptime(125) == "2м 5с"
    assert _fmt_uptime(3700) == "1ч 1м"
    assert _fmt_uptime(90000) == "1д 1ч"


def test_status_lines_no_session():
    lines = _status_lines("u-ux-status", False, 90)
    text = "\n".join(lines)
    assert "Отслеживается чатов: 0" in text
    assert "Фото: выключено" in text
    assert "Авто-перевод: 0" in text
    assert "Аптайм: 1м 30с" in text
    assert "/start" in text


def test_photo_line_modes():
    storage.photo_settings["u-ux-ph"] = {"enabled": True, "mode": "only_selected", "exceptions": [[1, 0], [2, 0]]}
    storage.photo_settings["u-ux-ph2"] = {"enabled": True, "mode": "all_except", "exceptions": [[1, 0]]}
    try:
        assert _photo_line("u-ux-ph") == "Фото: только выбранные (2)"
        assert _photo_line("u-ux-ph2") == "Фото: все, кроме (1)"
        assert _photo_line("u-ux-none") == "Фото: выключено"
    finally:
        storage.photo_settings.pop("u-ux-ph", None)
        storage.photo_settings.pop("u-ux-ph2", None)


def test_first_watch_hint():
    storage.watched_chats["u-ux-hint"] = [[-100, 0]]
    storage.watched_chats["u-ux-hint2"] = [[-100, 0], [-101, 0]]
    try:
        hint = _first_watch_hint("u-ux-hint")
        assert ".watched" in hint and ".unwatch" in hint
        assert _first_watch_hint("u-ux-hint2") == ""
        assert _first_watch_hint("u-ux-nobody") == ""
    finally:
        storage.watched_chats.pop("u-ux-hint", None)
        storage.watched_chats.pop("u-ux-hint2", None)


def test_help_sections_cover_all():
    for key in ("base", "session", "ai"):
        lines = _section_lines(key)
        assert len(lines) >= 5, key
        card = _render_section(key)
        assert card.startswith("<b>Slim bot | Help ·"), key
    assert _section_lines("nope") == []
    kb = help_keyboard(False)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "helpsec:base" in callbacks and "helpsec:session" in callbacks and "helpsec:ai" in callbacks


def test_helpsec_callback_renders_section():
    from handlers.commands.help import helpsec_cb

    edits = []

    async def edit_text(text, reply_markup=None):
        edits.append(text)

    async def answer(*args, **kwargs):
        pass

    msg = SimpleNamespace(edit_text=edit_text, from_user=None)
    cb = SimpleNamespace(data="helpsec:ai", from_user=SimpleNamespace(id=424245), message=msg, answer=answer)
    asyncio.run(helpsec_cb(cb))
    assert edits and ".ии" in edits[0]

    cb_all = SimpleNamespace(data="helpsec:all", from_user=SimpleNamespace(id=424245), message=msg, answer=answer)
    asyncio.run(helpsec_cb(cb_all))
    assert ".del" in edits[-1]
