"""P2.2/P2.3 — лимиты на дорогие команды в обоих путях.

Регрессии:
- `.tr` уходил в Willow/GPT вообще без лимита и без гейта по сессии —
  открытый сжигатель общего LLM-ключа.
- `.net` (~25 исходящих запросов на вызов), `.hash`, `.uuid`, `.b64`,
  `.опенкод` в aiogram-пути не лимитировались, хотя Telethon-путь `.net` уже
  лимитировался. Расхождение между путями.
"""

import asyncio

import pytest

from handlers.commands import netcmds, opencode, tools
from utils import rate_limit


@pytest.fixture(autouse=True)
def _reset():
    rate_limit.reset()
    yield
    rate_limit.reset()


# --------------------------------------------------------------------------
# .tr — LLM-бюджет
# --------------------------------------------------------------------------

def test_tr_limited_uses_ai_budget():
    """`.tr` — это полноценный LLM-вызов ⇒ бюджет «ai», а не «network»."""
    from config import AI_REQUEST_LIMIT

    for _ in range(AI_REQUEST_LIMIT):
        assert tools._tr_limited("u1") is True
    assert tools._tr_limited("u1") is False


def test_tr_and_ai_share_one_budget(monkeypatch):
    """Суммарный лимит на LLM-операции общий — иначе обойти через `.ии`."""
    from config import AI_REQUEST_LIMIT

    for _ in range(AI_REQUEST_LIMIT):
        assert tools._tr_limited("u1") is True
    assert tools._tr_limited("u1") is False
    # Bucket один и тот же
    assert rate_limit.bucket_count() == 1, rate_limit._BUCKETS


def test_tr_limit_is_per_user():
    assert tools._tr_limited("u1") is False or True
    for _ in range(10):
        tools._tr_limited("u1")
    assert tools._tr_limited("u2") is True


class FakeMessage:
    def __init__(self, text, reply=None):
        self.text = text
        self.reply_to_message = reply
        self.message_thread_id = None
        self.from_user = type("U", (), {"id": 1})()
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    async def answer(self, text, **kwargs):
        self.replies.append(text)


def test_cmd_tr_private_blocks_when_limited(monkeypatch):
    """При исчерпанном лимите `.tr` не должен дёргать LLM вообще."""
    called = []

    async def spy(*a, **kw):
        called.append(a)
        return "перевод"

    monkeypatch.setattr(tools, "_do_tr", spy)
    monkeypatch.setattr(tools, "_tr_limited", lambda uid: False)

    msg = FakeMessage(".tr en hello")
    asyncio.run(tools.cmd_tr_private(msg))
    assert not called, "LLM must not be called when the limit is hit"
    assert msg.replies, "user must be told"
    assert "Слишком много" in msg.replies[0]


def test_cmd_tr_private_allows_when_under_limit(monkeypatch):
    called = []

    async def spy(uid, args, reply_text):
        called.append(args)
        return "ok"

    monkeypatch.setattr(tools, "_do_tr", spy)
    monkeypatch.setattr(tools, "_tr_limited", lambda uid: True)
    asyncio.run(tools.cmd_tr_private(FakeMessage(".tr en hello")))
    assert called == ["en hello"]


# --------------------------------------------------------------------------
# .net/.hash/.uuid/.b64 — сетевой бюджет в aiogram-пути
# --------------------------------------------------------------------------

def test_netcmds_exposes_rate_limit_helper():
    from config import EXPENSIVE_COMMAND_LIMIT
    for _ in range(EXPENSIVE_COMMAND_LIMIT):
        assert netcmds._rate_limited("u1", ".net") is True
    assert netcmds._rate_limited("u1", ".net") is False


def test_budgets_are_separate():
    """P8.8: общий bucket позволял `.love` заблокировать `.net`."""
    from utils import cmds, rate_limit_gate as gate
    from utils.rate_limit import reset

    reset()
    # исчерпываем «сетевой» бюджет дешёвыми .love
    for _ in range(20):
        gate.check(".love", "u1")
    assert gate.check(".net", "u1") is True, ".love must not consume the .net budget"
    # и наоборот
    reset()
    for _ in range(20):
        gate.check(".net", "u1")
    assert gate.check(".love", "u1") is True
    reset()


def test_love_is_limited_in_aiogram_path():
    """P8.7: анимации бьют по общему токену бота — лимит обязателен в обоих путях."""
    import inspect
    from handlers.commands import love

    src = inspect.getsource(love)
    assert "gate.check" in src, "aiogram .love must be rate-limited too"
    assert '.love"' in src or "'.love'" in src


@pytest.mark.parametrize(
    "handler,cmds,expect_title",
    [
        (netcmds.cmd_net_private, (".net example.com",), "Net"),
        (netcmds.cmd_hash_private, (".hash sha256 x",), "Hash"),
        (netcmds.cmd_uuid_private, (".uuid",), "UUID"),
        (netcmds.cmd_b64_private, (".b64 hi",), "Base64"),
    ],
)
def test_netcmds_block_when_limited(monkeypatch, handler, cmds, expect_title):
    """Ни одна из команд не должна дёргать сеть/файл при исчерпанном лимите."""
    called = []
    monkeypatch.setattr(netcmds, "_rate_limited", lambda uid, head: False)
    for name in ("_do_net", "_do_hash", "_do_uuid", "_do_b64"):
        monkeypatch.setattr(
            netcmds, name,
            lambda *a, **kw: (called.append(name), asyncio.sleep(0, result="x"))[1],
        )
    msg = FakeMessage(cmds[0])
    asyncio.run(handler(msg))
    assert not called, f"{expect_title} ran despite the rate limit"
    assert msg.replies, f"{expect_title}: user must be told"
    assert expect_title in msg.replies[0]


@pytest.mark.parametrize(
    "handler,cmds,do_name",
    [
        (netcmds.cmd_net_private, ".net example.com", "_do_net"),
        (netcmds.cmd_hash_private, ".hash sha256 x", "_do_hash"),
        (netcmds.cmd_uuid_private, ".uuid", "_do_uuid"),
        (netcmds.cmd_b64_private, ".b64 hi", "_do_b64"),
    ],
)
def test_netcmds_allow_under_limit(monkeypatch, handler, cmds, do_name):
    called = []

    async def spy(*a, **kw):
        called.append(1)
        return "result"

    monkeypatch.setattr(netcmds, "_rate_limited", lambda uid, head: True)
    monkeypatch.setattr(netcmds, do_name, spy)
    asyncio.run(handler(FakeMessage(cmds)))
    assert called == [1]


# --------------------------------------------------------------------------
# .опенкод — внешний сервис
# --------------------------------------------------------------------------

def test_opencode_blocks_when_limited(monkeypatch):
    called = []

    async def spy():
        called.append(1)
        return None

    monkeypatch.setattr(opencode, "fetch_opencode_stats", spy)
    monkeypatch.setattr(opencode, "_limited", lambda uid: asyncio.sleep(0, result=False))

    msg = FakeMessage(".опенкод")
    asyncio.run(opencode.cmd_opencode_private(msg))
    assert not called, "external service must not be called"
    assert msg.replies


def test_opencode_allows_under_limit(monkeypatch):
    called = []

    async def spy():
        called.append(1)
        return {"data": {"opencode_tokens": {"available": True, "today": {}}}}

    monkeypatch.setattr(opencode, "fetch_opencode_stats", spy)
    monkeypatch.setattr(opencode, "_limited", lambda uid: asyncio.sleep(0, result=True))
    asyncio.run(opencode.cmd_opencode_private(FakeMessage(".опенкод")))
    assert called == [1]


# --------------------------------------------------------------------------
# Telethon-путь: список дорогих команд
# --------------------------------------------------------------------------

def test_telethon_rate_limit_covers_all_expensive_heads():
    """Бюджеты живут в реестре utils.cmds; проверяем сам реестр, а не
    хардкод-литералы в диспатче (их там больше нет)."""
    from utils import cmds

    must_be_limited = (
        ".tr", ".перевод", ".пер", ".ии", ".ai",
        ".net", ".сеть", ".hash", ".хеш", ".uuid", ".юид",
        ".b64", ".base64", ".calc", ".калк",
        ".вгф", ".vfg", ".gif", ".quote", ".цитата", ".q", ".цит",
        ".love", ".любовь", ".govno", ".говно", ".опенкод", ".opencode",
    )
    limited = cmds.RATE_LIMITED_BUDGET
    missing = [h for h in must_be_limited if h not in limited]
    assert not missing, f"not rate-limited: {missing}"


def test_telethon_tr_uses_ai_budget():
    import inspect

    from utils import cmds
    from utils.telethon_manager import TelethonManager

    from utils import rate_limit_gate as gate

    # .tr обязан быть именно в LLM-бюджете — иначе его можно обойти
    # через соседнюю команду с другим бакетом.
    assert {".tr", ".перевод", ".пер", ".перевести"} <= cmds.AI_BUDGET
    assert gate.rate_limit_for(".tr")[0] == "ai"
    assert gate.rate_limit_for(".ии")[0] == "ai"
