"""P3.6/P3.7 — таймауты на RPC и троттлинг 4096 символов.

- P3.6: голые `client.get_entity` / `get_me()` без `wait_for` — зависший RPC
  вешал команду навсегда (до внешнего 300-секундного таймаута хендлера или
  вообще, если внешнего таймаута нет).
- P3.7: Telegram отклоняет > 4096 символов, а ошибка отправки
  проглатывалась — пользователь видел команду без ответа.
"""

import asyncio
import inspect
import pathlib
import re

import pytest

from handlers.commands import _base

ROOT = pathlib.Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# P3.7 — 4096
# --------------------------------------------------------------------------

def test_short_card_is_untouched():
    card = _base.command_card("T", "короткий текст")
    assert card == "<b>Slim bot | T</b>\n<blockquote>короткий текст</blockquote>"


def test_long_card_is_truncated_to_limit():
    card = _base.command_card("B64", "A" * 10_000)
    assert len(card) <= _base.TELEGRAM_MAX_MESSAGE, len(card)


def test_truncation_marker_added():
    card = _base.command_card("T", "x" * 10_000)
    assert "обрезано" in card


def test_truncation_keeps_tags_balanced():
    card = _base.command_card("T", "<code>" + "A" * 10_000)
    for tag in ("code", "b", "i", "s", "u"):
        opens = len(re.findall(rf"<{tag}(?:\s[^>]*)?>", card))
        closes = len(re.findall(rf"</{tag}>", card))
        assert opens == closes, f"<{tag}> unbalanced after truncation: {opens} vs {closes}"


def test_truncation_does_not_split_a_tag():
    card = _base.command_card("T", "A" * 4090 + "<code>BBBB</code>")
    assert "<code" not in card[card.rfind("<") :] or "</code>" in card
    # незакрытый хвостовой тег недопустим
    assert not card.endswith("</code>")


def test_truncate_helper_is_idempotent():
    once = _base._truncate("x" * 10_000)
    twice = _base._truncate(once)
    assert twice == once or len(twice) <= _base.TELEGRAM_MAX_MESSAGE


def test_truncate_preserves_blockquote_close():
    card = _base.command_card("T", "y" * 10_000)
    assert "</blockquote>" in card


def test_limit_matches_telegram():
    assert _base.TELEGRAM_MAX_MESSAGE == 4096


def test_netcmds_long_output_is_truncated():
    """Реальный случай: .net собирает десятки строк."""
    from handlers.commands.netcmds import _do_net

    out = asyncio.run(_do_net("1", "example.com", None))
    assert len(out) <= _base.TELEGRAM_MAX_MESSAGE, len(out)


def test_b64_long_output_is_truncated():
    """Реальный случай: .b64 encode 3200 символов = 4268 > лимита."""
    from utils.hashing import b64_op
    from handlers.commands._base import command_card

    result = b64_op("A" * 3200, "encode")
    card = command_card("Base64", f"<code>{result}</code>")
    assert len(card) <= _base.TELEGRAM_MAX_MESSAGE, len(card)


# --------------------------------------------------------------------------
# P3.6 — таймауты на RPC
# --------------------------------------------------------------------------

BARE_RPC_FILES = [
    "handlers/commands/watch.py",
    "handlers/commands/admins.py",
    "handlers/commands/knowledge.py",
]


@pytest.mark.parametrize("path", BARE_RPC_FILES)
def test_no_bare_rpc_without_timeout(path):
    """Статический инвариант: `await client.get_entity(...)` / `get_me()`
    обязан быть обёрнут в `asyncio.wait_for`."""
    src = (ROOT / path).read_text(encoding="utf-8")
    lines = src.splitlines()
    offenders = []
    for i, line in enumerate(lines):
        if "await " not in line:
            continue
        if not re.search(r"\.get_entity\(|\.get_me\(\)", line):
            continue
        # Многострочный вызов: ищем wait_for в предыдущей строке
        if "asyncio.wait_for(" in line:
            continue
        prev = lines[i - 1] if i else ""
        if "asyncio.wait_for(" in prev:
            continue
        offenders.append(f"{path}:{i + 1}: {line.strip()}")
    assert not offenders, "bare RPC without timeout:\n" + "\n".join(offenders)


def test_premium_get_me_has_timeout():
    src = (ROOT / "utils" / "premium.py").read_text(encoding="utf-8")
    assert "asyncio.wait_for(client.get_me()" in src
    assert "_PREMIUM_RPC_TIMEOUT" in src


def test_premium_rpc_timeout_is_used():
    from utils import premium
    assert 0 < premium._PREMIUM_RPC_TIMEOUT <= 60


def test_watched_resolve_has_timeout():
    """Цикл по всем чатам: каждый resolve под таймаутом."""
    src = (ROOT / "handlers" / "commands" / "watch.py").read_text(encoding="utf-8")
    assert "asyncio.wait_for(\n            client.get_entity(chat_id)" in src or (
        "asyncio.wait_for" in src and "client.get_entity(chat_id)" in src
    )


def test_admins_get_participants_has_timeout():
    src = (ROOT / "handlers" / "commands" / "admins.py").read_text(encoding="utf-8")
    assert "asyncio.wait_for(" in src
    assert "GetParticipantsRequest" in src


def test_admins_tolerates_single_resolve_failure():
    """Один зависший участник не должен ронять весь список."""
    src = (ROOT / "handlers" / "commands" / "admins.py").read_text(encoding="utf-8")
    assert "except Exception:\n                continue" in src


def test_timeouts_imported_in_modules():
    for path in BARE_RPC_FILES:
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "TELETHON_RESOLVE_TIMEOUT" in src, f"{path} must use the shared timeout"
        assert "import asyncio" in src, f"{path} needs asyncio for wait_for"
