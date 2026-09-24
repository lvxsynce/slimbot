import asyncio
import json

import pytest

from utils.escape import esc
from utils.storage import was_processed
from utils.url_safety import UnsafeURL, validate_public_url


def test_html_escape_covers_telegram_markup():
    assert esc('<b>x</b> & "q"') == "&lt;b&gt;x&lt;/b&gt; &amp; \"q\""
    assert esc("'", quote=True) == "&#x27;"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "file:///etc/passwd",
        "http://user:password@example.com/",
    ],
)
def test_private_and_non_http_urls_are_rejected(url):
    with pytest.raises(UnsafeURL):
        asyncio.run(validate_public_url(url))


def test_duplicate_guard_is_thread_aware_and_has_ttl_origin():
    chat_id = 987654321
    msg_id = 123
    assert was_processed(chat_id, msg_id, 1) is False
    assert was_processed(chat_id, msg_id, 1) is True
    assert was_processed(chat_id, msg_id, 2) is False


def test_storage_read_json_recovers_from_backup(tmp_path):
    from utils.storage import _read_json

    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    path.with_suffix(".json.bak").write_text(
        json.dumps({"ok": True}), encoding="utf-8"
    )
    assert _read_json(path, {}) == {"ok": True}


def test_session_exists_normalizes_integer_user_id(monkeypatch, tmp_path):
    import utils.storage as storage

    uid = "12345"
    session_file = tmp_path / "session.session"
    session_file.touch()
    monkeypatch.setattr(storage, "user_sessions", {uid: {"status": "active"}})
    monkeypatch.setattr(storage, "session_path", lambda value: str(session_file.with_suffix("")))

    assert storage.session_exists(12345) is True


def test_telethon_session_gate_allows_only_active_owners(monkeypatch):
    import utils.storage as storage

    monkeypatch.setattr(storage, "session_exists", lambda user_id: str(user_id) == "owner")
    assert storage.session_exists("owner") is True
    assert storage.session_exists("other") is False


def test_text_templates_have_external_title_and_no_emoji():
    from utils.texts import Texts

    rendered = Texts.Ping.PING.render(ms="12")
    assert rendered.startswith("<b>Slim bot | Ping</b>")
    assert rendered.count("<blockquote>") == 1
    assert "🏓" not in rendered


def test_runtime_paths_are_not_cwd_relative():
    import config

    assert config.DATA_DIR.is_absolute()
    assert config.SESSIONS_DIR.is_absolute()
    assert config.USER_SESSIONS_FILE.is_absolute()


def test_auth_attempt_limiter_enforces_cooldown(monkeypatch):
    import handlers.session as session

    uid = "test-auth-limiter"
    session._AUTH_ATTEMPTS.pop(uid, None)
    monkeypatch.setattr(session, "AUTH_COOLDOWN", 60)
    assert session._allow_auth_attempt(uid) is True
    assert session._allow_auth_attempt(uid) is False
    session._AUTH_ATTEMPTS.pop(uid, None)


def test_expensive_command_limiter_is_bounded():
    from utils.rate_limit import allow, clear

    uid = "test-expensive-limiter"
    clear(uid)
    assert allow(uid, "network", limit=2, window=60) is True
    assert allow(uid, "network", limit=2, window=60) is True
    assert allow(uid, "network", limit=2, window=60) is False
    clear(uid)


def test_telethon_health_check_reports_unhealthy_client(monkeypatch):
    from utils.telethon_manager import TelethonManager

    class HealthyClient:
        def is_connected(self):
            return True

        async def get_me(self):
            return object()

    class BrokenClient:
        def is_connected(self):
            return True

        async def get_me(self):
            raise RuntimeError("connection lost")

        async def disconnect(self):
            return None

    manager = TelethonManager()
    manager._clients = {"healthy": HealthyClient(), "broken": BrokenClient()}
    monkeypatch.setattr("utils.telethon_manager.TELETHON_HEALTH_TIMEOUT", 1)

    result = asyncio.run(manager.check_clients_health())

    assert result == {"healthy": True, "broken": False}
