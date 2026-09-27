"""Тесты улучшенного .net: multi-DNS, title, probe (без реальной сети)."""

import asyncio

import dns.resolver

from utils.netinfo import dns_multi, extract_title, fetch_title, http_probe
from handlers.commands.netcmds import _do_net


def test_extract_title_basic():
    assert extract_title("<html><head><title>  Hello  World </title></head>") == "Hello World"


def test_extract_title_entities_and_case():
    assert extract_title("<TITLE>A &amp; B</TITLE>") == "A & B"


def test_extract_title_multiline():
    assert extract_title("<title>\n  Line1\n  Line2\n</title>") == "Line1 Line2"


def test_extract_title_missing_or_empty():
    assert extract_title("<html><body>no title</body></html>") is None
    assert extract_title("") is None
    assert extract_title(None) is None
    assert extract_title("<title>   </title>") is None


def test_extract_title_truncated():
    assert len(extract_title("<title>" + "x" * 500 + "</title>")) == 300


class _FakeRecord:
    def __init__(self, text):
        self._text = text

    def to_text(self):
        return self._text


class _FakeResolver:
    instances = []

    def __init__(self, configure=True):
        self.nameservers = []
        self.timeout = 0
        self.lifetime = 0
        _FakeResolver.instances.append(self)

    async def resolve(self, host, rtype):
        ns = self.nameservers[0] if self.nameservers else None
        if ns == "9.9.9.9":
            raise dns.resolver.NXDOMAIN()
        return [_FakeRecord("93.184.216.34" if ns != "8.8.8.8" else "93.184.216.35")]


def test_dns_multi_structure(monkeypatch):
    _FakeResolver.instances.clear()
    monkeypatch.setattr("dns.asyncresolver.Resolver", _FakeResolver)
    res = asyncio.run(dns_multi("example.com"))
    assert set(res) == {"Cloudflare", "Google", "Quad9", "Система"}
    assert res["Cloudflare"]["ips"] == ["93.184.216.34"]
    assert res["Cloudflare"]["error"] is None
    assert isinstance(res["Cloudflare"]["ms"], int)
    assert res["Google"]["ips"] == ["93.184.216.35"]
    assert res["Quad9"]["ips"] is None
    assert res["Quad9"]["error"] == "NXDOMAIN"
    assert res["Система"]["ips"] == ["93.184.216.34"]


def test_fetch_title_rejects_private_without_network():
    res = asyncio.run(fetch_title("http://127.0.0.1/"))
    assert res["title"] is None
    assert res["error"]


def test_http_probe_rejects_private_without_network():
    res = asyncio.run(http_probe("127.0.0.1"))
    assert res["status"] is None
    assert res["error"]


def test_do_net_usage_no_network():
    out = asyncio.run(_do_net("u", ""))
    assert ".net" in out
