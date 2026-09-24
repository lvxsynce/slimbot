"""DNS, IP info, unshorten — всё в одном."""
import asyncio
import ipaddress
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
