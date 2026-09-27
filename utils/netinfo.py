"""DNS, IP info, unshorten — всё в одном."""
import asyncio
import html as _html
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import aiohttp
import dns.asyncresolver
import dns.resolver

from config import (
    IPINFO_URL_TEMPLATE,
    NETINFO_TIMEOUT_TOTAL,
    NETINFO_TIMEOUT_CONNECT,
    DEFAULT_USER_AGENT as UA,
    NET_PASSIVE_TIMEOUT,
    NET_PASSIVE_MAX_NAMES,
)
from utils.url_safety import UnsafeURL, validate_public_url

TIMEOUT = aiohttp.ClientTimeout(total=NETINFO_TIMEOUT_TOTAL, connect=NETINFO_TIMEOUT_CONNECT)
PASSIVE_TIMEOUT = aiohttp.ClientTimeout(total=NET_PASSIVE_TIMEOUT, connect=5)

DNS_TYPES = ("A", "AAAA", "MX", "TXT", "NS", "CNAME", "SOA", "CAA", "PTR")

# Публичные DNS для сверки доступности с разных резолверов.
# (host, None) = системный резолвер.
PUBLIC_RESOLVERS: tuple[tuple[str, str | None], ...] = (
    ("Cloudflare", "1.1.1.1"),
    ("Google", "8.8.8.8"),
    ("Quad9", "9.9.9.9"),
    ("Система", None),
)

TITLE_MAX_BYTES = 256 * 1024


async def dns_lookup(host: str, rtype: str = "A") -> list[str] | str:
    """Возвращает list строк или str с ошибкой."""
    rtype = rtype.upper()
    if rtype not in DNS_TYPES:
        return f"Тип неизвестен. Доступно: {', '.join(DNS_TYPES)}"
    try:
        resolver = dns.asyncresolver.Resolver()
        resolver.timeout = 5
        resolver.lifetime = 8
        ans = await resolver.resolve(host, rtype)
    except dns.resolver.NXDOMAIN:
        return "NXDOMAIN"
    except dns.resolver.NoAnswer:
        return f"Нет записи {rtype}"
    except dns.resolver.Timeout:
        return "Таймаут"
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return [r.to_text() for r in ans]


async def _resolve_via(host: str, rtype: str, nameserver: str | None, timeout: float = 5.0) -> tuple[list[str] | None, str | None, int]:
    """Резолвит host через один nameserver (None = системный).
    Возвращает (ips | None, error | None, ms)."""
    import time as _time

    t0 = _time.monotonic()
    try:
        if nameserver is None:
            resolver = dns.asyncresolver.Resolver()
        else:
            resolver = dns.asyncresolver.Resolver(configure=False)
            resolver.nameservers = [nameserver]
        resolver.timeout = timeout
        resolver.lifetime = timeout

        async def _query():
            ans = await resolver.resolve(host, rtype)
            return [r.to_text() for r in ans]

        ips = await asyncio.wait_for(_query(), timeout=timeout + 1)
        return ips, None, int((_time.monotonic() - t0) * 1000)
    except asyncio.TimeoutError:
        return None, "таймаут", int((_time.monotonic() - t0) * 1000)
    except dns.resolver.NXDOMAIN:
        return None, "NXDOMAIN", int((_time.monotonic() - t0) * 1000)
    except dns.resolver.NoAnswer:
        return None, f"нет {rtype}", int((_time.monotonic() - t0) * 1000)
    except Exception as e:
        return None, f"{type(e).__name__}", int((_time.monotonic() - t0) * 1000)


async def dns_multi(host: str, rtype: str = "A") -> dict[str, dict]:
    """A/AAAA-записи хоста глазами разных DNS-резолверов.

    Позволяет заметить рассинхрон (geo-DNS, кэш, подмена).
    Возвращает {label: {"ips": [...] | None, "error": str | None, "ms": int}}.
    """
    results = await asyncio.gather(
        *(_resolve_via(host, rtype.upper(), ns) for _, ns in PUBLIC_RESOLVERS),
        return_exceptions=True,
    )
    out: dict[str, dict] = {}
    for (label, _), res in zip(PUBLIC_RESOLVERS, results):
        if isinstance(res, BaseException):
            out[label] = {"ips": None, "error": type(res).__name__, "ms": 0}
        else:
            ips, error, ms = res
            out[label] = {"ips": ips, "error": error, "ms": ms}
    return out


async def http_probe(host: str) -> dict:
    """Доступность сайта: пробует https://host, затем http://host.

    Возвращает {"url", "status" | None, "ms", "server", "error" | None}.
    Тело не читается — только HEAD (GET при 405/501).
    """
    import time as _time

    last_error: str | None = None
    for scheme in ("https", "http"):
        url = f"{scheme}://{host}"
        t0 = _time.monotonic()
        try:
            await validate_public_url(url)
            async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as sess:
                try:
                    async with sess.head(url, allow_redirects=False, ssl=False) as r:
                        if r.status in (405, 501):
                            raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status)
                        return {
                            "url": url,
                            "status": r.status,
                            "ms": int((_time.monotonic() - t0) * 1000),
                            "server": r.headers.get("Server", ""),
                            "error": None,
                        }
                except aiohttp.ClientResponseError:
                    async with sess.get(url, allow_redirects=False, ssl=False) as r:
                        return {
                            "url": url,
                            "status": r.status,
                            "ms": int((_time.monotonic() - t0) * 1000),
                            "server": r.headers.get("Server", ""),
                            "error": None,
                        }
        except UnsafeURL as e:
            return {"url": url, "status": None, "ms": 0, "server": "", "error": str(e)}
        except asyncio.TimeoutError:
            last_error = "таймаут"
        except (aiohttp.ClientConnectorError, aiohttp.ClientError) as e:
            last_error = f"не подключиться: {e.os_error or e}" if isinstance(e, aiohttp.ClientConnectorError) else f"{type(e).__name__}"
            if scheme == "https":
                continue
        except Exception as e:
            last_error = f"{type(e).__name__}"
            if scheme == "https":
                continue
    return {"url": f"https://{host}", "status": None, "ms": 0, "server": "", "error": last_error or "недоступен"}


def _normalize(url: str) -> str:
    from utils.linkcheck import _normalize as normalize_url
    return normalize_url(url)


async def unshorten(url: str, max_hops: int = 15) -> list[str] | str:
    """Только редиректы. Возвращает list URLs или str с ошибкой."""
    url = _normalize(url)
    chain = [url]
    error = None
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as sess:
            current = url
            for _ in range(max_hops):
                await validate_public_url(current)
                try:
                    async with sess.head(current, allow_redirects=False, ssl=False) as r:
                        if r.status in (301, 302, 303, 307, 308):
                            loc = r.headers.get("Location")
                            if not loc:
                                break
                            current = str(r.url.join(aiohttp.client.URL(loc)))
                            chain.append(current)
                            continue
                        break
                except aiohttp.ClientResponseError:
                    async with sess.get(current, allow_redirects=False, ssl=False) as r:
                        if r.status in (301, 302, 303, 307, 308):
                            loc = r.headers.get("Location")
                            if not loc:
                                break
                            current = str(r.url.join(aiohttp.client.URL(loc)))
                            chain.append(current)
                            continue
                        break
            else:
                error = "слишком много редиректов"
    except asyncio.TimeoutError:
        error = "таймаут"
    except UnsafeURL as e:
        error = str(e)
    except aiohttp.ClientError as e:
        error = f"{type(e).__name__}: {e}"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    if error and len(chain) == 1:
        return error
    return chain


def _resolve_ip(value: str) -> str | None:
    try:
        socket.inet_aton(value)
        return value
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, value)
        return value
    except OSError:
        pass
    try:
        return socket.gethostbyname(value)
    except (socket.gaierror, OSError):
        return None


async def ip_info(target: str) -> dict:
    """ipinfo.io — free, 50k/мес без ключа."""
    ip = await asyncio.to_thread(_resolve_ip, target)
    if not ip:
        return {"error": f"не могу разрезолвить: {target}"}
    url = IPINFO_URL_TEMPLATE.format(ip=ip)
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    return {"error": f"ipinfo вернул {r.status}", "ip": ip}
                data = await r.json(content_type=None)
                data["resolved_from"] = target if target != ip else None
                return data
    except asyncio.TimeoutError:
        return {"error": "таймаут", "ip": ip}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "ip": ip}


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def extract_title(html_text: str | None) -> str | None:
    """Достаёт <title> из HTML. Чистая функция — покрыта тестами."""
    if not html_text:
        return None
    m = _TITLE_RE.search(html_text)
    if not m:
        return None
    title = re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()
    return title[:300] or None


async def fetch_title(url: str, max_hops: int = 5) -> dict:
    """Заголовок страницы: следует до max_hops редиректов, читает до 256KB.

    Только text/html — остальное возвращает без title (не ошибка).
    Каждый хоп проверяется через validate_public_url (SSRF-защита).
    Возвращает {"title" | None, "final_url", "error" | None}.
    """
    try:
        url = _normalize(url)
    except UnsafeURL as e:
        return {"title": None, "final_url": url, "error": str(e)}
    final_url = url
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}) as sess:
            current = url
            for _ in range(max_hops):
                await validate_public_url(current)
                async with sess.get(current, allow_redirects=False, ssl=False) as r:
                    if r.status in (301, 302, 303, 307, 308):
                        loc = r.headers.get("Location")
                        if not loc:
                            break
                        current = str(r.url.join(aiohttp.client.URL(loc)))
                        final_url = current
                        continue
                    if r.status < 200 or r.status >= 300:
                        return {"title": None, "final_url": current, "error": f"HTTP {r.status}"}
                    ctype = (r.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
                    if ctype and "html" not in ctype:
                        return {"title": None, "final_url": current, "error": None}
                    raw = await r.content.read(TITLE_MAX_BYTES + 1)
                    try:
                        text = raw.decode(r.get_encoding() or "utf-8", errors="replace")
                    except Exception:
                        text = raw.decode("utf-8", errors="replace")
                    return {"title": extract_title(text), "final_url": current, "error": None}
            return {"title": None, "final_url": final_url, "error": "слишком много редиректов"}
    except asyncio.TimeoutError:
        return {"title": None, "final_url": final_url, "error": "таймаут"}
    except UnsafeURL as e:
        return {"title": None, "final_url": final_url, "error": str(e)}
    except (aiohttp.ClientConnectorError, aiohttp.ClientError) as e:
        return {"title": None, "final_url": final_url, "error": "не подключиться"}
    except Exception as e:
        return {"title": None, "final_url": final_url, "error": f"{type(e).__name__}"}


def _same_domain(name: str, domain: str) -> bool:
    name = name.rstrip(".").lower()
    domain = domain.rstrip(".").lower()
    return name == domain or name.endswith("." + domain)


async def _resolve_public_ips(host: str) -> list[str]:
    """Resolve only public A/AAAA records for one passive-discovery hostname."""
    values: set[str] = set()
    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = 3
    resolver.lifetime = 5
    for record_type in ("A", "AAAA"):
        try:
            answer = await resolver.resolve(host, record_type)
        except Exception:
            continue
        for record in answer:
            value = str(record).strip()
            try:
                if ipaddress.ip_address(value).is_global:
                    values.add(value)
            except ValueError:
                continue
    return sorted(values)


async def passive_related_ips(domain: str) -> dict:
    """Find publicly disclosed related IPs without probing hosts or ports.

    Sources are DNS MX/NS records and Certificate Transparency names from
    crt.sh. Results are candidates only: none is claimed to be the origin.
    """
    domain = domain.rstrip(".").lower()
    names: dict[str, set[str]] = {domain: {"current DNS"}}
    errors: list[str] = []

    async def add_record_hosts(record_type: str, source: str) -> None:
        result = await dns_lookup(domain, record_type)
        if not isinstance(result, list):
            return
        for value in result:
            host = value.rstrip(".").split()[-1].rstrip(".").lower()
            if _same_domain(host, domain):
                names.setdefault(host, set()).add(source)

    await asyncio.gather(
        add_record_hosts("MX", "MX"),
        add_record_hosts("NS", "NS"),
    )

    try:
        async with aiohttp.ClientSession(timeout=PASSIVE_TIMEOUT, headers={"User-Agent": UA}) as sess:
            async with sess.get(
                "https://crt.sh/",
                params={"q": f"%.{domain}", "output": "json"},
                ssl=True,
            ) as response:
                if response.status == 200:
                    payload = await response.json(content_type=None)
                    for row in payload if isinstance(payload, list) else []:
                        for raw_name in str(row.get("name_value", "")).splitlines():
                            host = raw_name.strip().lstrip("*.").rstrip(".").lower()
                            if host and _same_domain(host, domain):
                                names.setdefault(host, set()).add("crt.sh")
                else:
                    errors.append(f"crt.sh HTTP {response.status}")
    except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as exc:
        errors.append(f"crt.sh: {type(exc).__name__}")
    except Exception as exc:
        errors.append(f"crt.sh: {type(exc).__name__}")

    candidates: dict[str, dict] = {}
    ordered_names = list(names)[: max(1, NET_PASSIVE_MAX_NAMES)]
    resolved = await asyncio.gather(
        *(_resolve_public_ips(host) for host in ordered_names),
        return_exceptions=True,
    )
    for host, values in zip(ordered_names, resolved):
        if isinstance(values, BaseException):
            continue
        for value in values:
            item = candidates.setdefault(value, {"ip": value, "hosts": set(), "sources": set()})
            item["hosts"].add(host)
            item["sources"].update(names[host])
    return {
        "domain": domain,
        "current_ips": await _resolve_public_ips(domain),
        "candidates": [
            {
                "ip": value["ip"],
                "hosts": sorted(value["hosts"])[:4],
                "sources": sorted(value["sources"]),
            }
            for value in sorted(candidates.values(), key=lambda item: item["ip"])
        ],
        "errors": errors,
    }
