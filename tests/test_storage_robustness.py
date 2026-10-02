"""P3.8/P3.9/P3.11 — битые JSON-файлы не должны ронять импорт и обработку.

- P3.8: `load_photo_settings` не проверял тип. Список вместо dict ⇒
  `photo_settings.get(...)` кидал AttributeError прямо в обработчике
  входящих — view-once молча переставал сохраняться у юзера НАВСЕГДА.
- P3.9: `load_user_tz` и `load_watched` тоже не гардили тип верхнего уровня.
- P3.11: `watch._auto_enable_photos` ходил по ключам напрямую (`s["mode"]`),
  поэтому запись без `exceptions` давала KeyError ПОСЛЕ того, как чат уже
  добавили в watched — тихая поломка.
"""

import json
import pathlib

import pytest

from utils import storage as S


@pytest.fixture(autouse=True)
def _restore():
    saved = {
        "photo": dict(S.photo_settings),
        "tz": dict(S.user_timezones),
        "watched": dict(S.watched_chats),
    }
    yield
    S.photo_settings.clear()
    S.photo_settings.update(saved["photo"])
    S.user_timezones.clear()
    S.user_timezones.update(saved["tz"])
    S.watched_chats.clear()
    S.watched_chats.update(saved["watched"])


def _write(path: pathlib.Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# P3.8 — photo_settings
# --------------------------------------------------------------------------

def test_photo_settings_rejects_top_level_list(tmp_path, monkeypatch):
    f = tmp_path / "photo_settings.json"
    _write(f, ["not", "a", "dict"])
    monkeypatch.setattr(S, "PHOTO_SETTINGS_FILE", f)
    S.load_photo_settings()
    assert isinstance(S.photo_settings, dict)
    assert S.photo_settings == {}


def test_photo_settings_default_is_usable_after_bad_file(tmp_path, monkeypatch):
    """Главный сценарий: после битого файла is_photo_allowed не падает."""
    f = tmp_path / "photo_settings.json"
    _write(f, [1, 2, 3])
    monkeypatch.setattr(S, "PHOTO_SETTINGS_FILE", f)
    S.load_photo_settings()
    assert S.is_photo_allowed("u1", -100) is False
    assert S.get_photo_settings("u1") == {"enabled": False, "mode": "all", "exceptions": []}


def test_photo_settings_normalizes_missing_fields(tmp_path, monkeypatch):
    """Запись без mode/exceptions/enabled нормализуется, а не KeyError."""
    f = tmp_path / "photo_settings.json"
    _write(f, {"u1": {"enabled": True}})
    monkeypatch.setattr(S, "PHOTO_SETTINGS_FILE", f)
    S.load_photo_settings()
    cfg = S.get_photo_settings("u1")
    assert cfg["enabled"] is True
    assert cfg["mode"] in ("all", "all_except", "only_selected")
    assert isinstance(cfg["exceptions"], list)


def test_photo_settings_rejects_unknown_mode(tmp_path, monkeypatch):
    f = tmp_path / "photo_settings.json"
    _write(f, {"u1": {"enabled": True, "mode": "какая-то дичь"}})
    monkeypatch.setattr(S, "PHOTO_SETTINGS_FILE", f)
    S.load_photo_settings()
    assert S.get_photo_settings("u1")["mode"] == "all"


def test_photo_settings_skips_non_dict_values(tmp_path, monkeypatch):
    f = tmp_path / "photo_settings.json"
    _write(f, {"u1": "строка", "u2": {"enabled": True}})
    monkeypatch.setattr(S, "PHOTO_SETTINGS_FILE", f)
    S.load_photo_settings()
    assert "u1" not in S.photo_settings
    assert "u2" in S.photo_settings


def test_photo_settings_keeps_valid_modes(tmp_path, monkeypatch):
    f = tmp_path / "photo_settings.json"
    _write(f, {
        "a": {"enabled": True, "mode": "all_except", "exceptions": [[-100, 0]]},
        "b": {"enabled": True, "mode": "only_selected", "exceptions": [[-200, 3]]},
    })
    monkeypatch.setattr(S, "PHOTO_SETTINGS_FILE", f)
    S.load_photo_settings()
    assert S.get_photo_settings("a")["mode"] == "all_except"
    assert S.get_photo_settings("b")["mode"] == "only_selected"
    assert S.is_photo_allowed("b", -200, 3) is True


# --------------------------------------------------------------------------
# P3.9 — остальные загрузчики
# --------------------------------------------------------------------------

def test_user_tz_rejects_list(tmp_path, monkeypatch):
    f = tmp_path / "user_timezones.json"
    _write(f, ["+3"])
    monkeypatch.setattr(S, "USER_TZ_FILE", f)
    S.load_user_tz()
    assert S.user_timezones == {}


def test_user_tz_keeps_strings(tmp_path, monkeypatch):
    f = tmp_path / "user_timezones.json"
    _write(f, {"u1": "+3", "u2": 5, "u3": "Europe/Moscow"})
    monkeypatch.setattr(S, "USER_TZ_FILE", f)
    S.load_user_tz()
    assert S.user_timezones == {"u1": "+3", "u3": "Europe/Moscow"}


def test_watched_rejects_list(tmp_path, monkeypatch):
    f = tmp_path / "watched_chats.json"
    _write(f, [1, 2])
    monkeypatch.setattr(S, "WATCHED_CHATS_FILE", f)
    S.load_watched()
    assert S.watched_chats == {}


def test_watched_rejects_scalar_values(tmp_path, monkeypatch):
    """Значение не-list ⇒ пустой список, а не падение на _migrate_watched."""
    f = tmp_path / "watched_chats.json"
    _write(f, {"u1": 5, "u2": [[-100, 0]]})
    monkeypatch.setattr(S, "WATCHED_CHATS_FILE", f)
    S.load_watched()
    assert S.watched_chats.get("u1") == []
    assert S.watched_chats.get("u2") == [[-100, 0]]


def test_watched_survives_garbage_elements(tmp_path, monkeypatch):
    """Мусорные элементы пропускаются, валидные сохраняются.

    Старый плоский формат — это голое число (`-100`), а не список из одного
    элемента, поэтому `"мусор"` и `[42]` оба невалидны и отбрасываются.
    """
    f = tmp_path / "watched_chats.json"
    _write(f, {"u1": [[-100, 0], "мусор", [42]], "u2": [-200, -300]})
    monkeypatch.setattr(S, "WATCHED_CHATS_FILE", f)
    monkeypatch.setattr(S, "save_watched", lambda: None)
    S.load_watched()
    assert S.watched_chats.get("u1") == [[-100, 0]]
    # старый плоский формат мигрирует в [[chat, 0]]
    assert S.watched_chats.get("u2") == [[-200, 0], [-300, 0]]


def test_user_sessions_rejects_list(tmp_path, monkeypatch):
    f = tmp_path / "user_sessions.json"
    _write(f, [{"phone": "+7"}])
    monkeypatch.setattr(S, "USER_SESSIONS_FILE", f)
    S.load_user_sessions()
    assert S.user_sessions == {}


def test_templates_rejects_list(tmp_path, monkeypatch):
    f = tmp_path / "templates.json"
    _write(f, ["a"])
    monkeypatch.setattr(S, "TEMPLATES_FILE", f)
    S.load_templates()
    assert S.templates == {}


def test_knowledge_settings_rejects_list(tmp_path, monkeypatch):
    f = tmp_path / "knowledge_settings.json"
    _write(f, [1])
    monkeypatch.setattr(S, "KNOWLEDGE_SETTINGS_FILE", f)
    S.load_knowledge_settings()
    assert S.knowledge_settings == {}


def test_auto_tr_chats_rejects_list(tmp_path, monkeypatch):
    f = tmp_path / "auto_tr_chats.json"
    _write(f, ["x"])
    monkeypatch.setattr(S, "AUTO_TR_CHATS_FILE", f)
    S.load_auto_tr_chats()
    assert S.auto_tr_chats == {}


def test_nya_chats_rejects_list(tmp_path, monkeypatch):
    f = tmp_path / "nya_chats.json"
    _write(f, ["x"])
    monkeypatch.setattr(S, "NYA_CHATS_FILE", f)
    S.load_nya_chats()
    assert S.nya_chats == {}


# --------------------------------------------------------------------------
# P3.11 — watch._auto_enable_photos не падает на битых settings
# --------------------------------------------------------------------------

def test_auto_enable_photos_tolerates_missing_keys(monkeypatch):
    """Раньше `s["exceptions"]` давал KeyError ПОСЛЕ add_chat_for_user —
    чат добавлен, авто-включение сломалось, юзер не получил сообщения."""
    from handlers.commands import watch as W

    S.photo_settings["u1"] = {"enabled": True}  # без mode/exceptions
    monkeypatch.setattr(W, "session_exists", lambda uid: True)
    saved = []
    monkeypatch.setattr(
        W, "update_photo_settings",
        lambda u, s: saved.append(s) or S.photo_settings.__setitem__(u, s),
    )
    # Главное — не бросает KeyError (раньше роняло всё после add_chat_for_user).
    W._auto_enable_photos("u1", -100, 0)

    S.photo_settings["u1"] = {"enabled": False}  # без mode/exceptions
    W._auto_enable_photos("u1", -200, 0)
    assert saved, "disabled branch must normalise and persist a fresh config"
    assert saved[0]["mode"] in ("all", "all_except", "only_selected")
    assert saved[0]["exceptions"] == [[-200, 0]]


def test_auto_enable_photos_only_selected_branch(monkeypatch):
    from handlers.commands import watch as W

    S.photo_settings["u1"] = {
        "enabled": True, "mode": "only_selected", "exceptions": [],
    }
    monkeypatch.setattr(W, "session_exists", lambda uid: True)
    monkeypatch.setattr(
        W, "update_photo_settings",
        lambda u, s: S.photo_settings.__setitem__(u, s),
    )
    W._auto_enable_photos("u1", -100, 0)
    cfg = S.get_photo_settings("u1")
    assert cfg["exceptions"] == [[-100, 0]], cfg


def test_disable_photos_keeps_other_exceptions(monkeypatch):
    """Регрессия: режим "all" перезаписывал dict и терял все исключения."""
    from handlers.commands import watch as W

    S.photo_settings["u1"] = {
        "enabled": True, "mode": "all", "exceptions": [[-500, 0], [-600, 7]],
    }
    monkeypatch.setattr(W, "session_exists", lambda uid: True)
    monkeypatch.setattr(
        W, "update_photo_settings",
        lambda u, s: S.photo_settings.__setitem__(u, s),
    )
    W._disable_photos_for_chat("u1", -100, 0)
    cfg = S.get_photo_settings("u1")
    assert cfg["mode"] == "all_except"
    assert [-500, 0] in cfg["exceptions"], "existing exceptions must survive"
    assert [-600, 7] in cfg["exceptions"], "existing exceptions must survive"
    assert [-100, 0] in cfg["exceptions"]
