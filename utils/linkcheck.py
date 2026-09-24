import asyncio
import re
import socket
import ssl
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from config import (
    LINKCHECK_TIMEOUT_TOTAL,
    LINKCHECK_TIMEOUT_CONNECT,
    MAX_REDIRECTS,
    DEFAULT_USER_AGENT as UA,
)
from utils.escape import esc
from utils.url_safety import UnsafeURL, validate_public_url

TIMEOUT = aiohttp.ClientTimeout(total=LINKCHECK_TIMEOUT_TOTAL, connect=LINKCHECK_TIMEOUT_CONNECT)

SUSPICIOUS_TLDS = {
    "zip", "mov", "xyz", "top", "click", "tk", "ml", "ga", "cf", "gq",
    "country", "stream", "loan", "work", "cricket", "review",
}
SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "buff.ly",
    "is.gd", "cutt.ly", "rebrand.ly", "s.id", "lnkd.in", "shorturl.at",
    "tiny.cc", "rb.gy", "v.gd", "t.ly",
}


def _normalize(url: str) -> str:
    url = url.strip().strip("<>\"'")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "http://" + url
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURL("разрешены только HTTP и HTTPS URL")
    if parsed.username or parsed.password:
        raise UnsafeURL("URL с учётными данными запрещены")
    if len(url) > 2048:
        raise UnsafeURL("URL слишком длинный")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURL("некорректный порт") from exc
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, parsed.fragment))


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _kind(content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return "—"
    mapping = {
        "text/html": "HTML",
        "application/json": "JSON",
        "application/xml": "XML",
        "text/xml": "XML",
        "text/plain": "текст",
        "application/pdf": "PDF",
        "application/zip": "ZIP",
        "application/octet-stream": "бинарь",
    }
    if ct in mapping:
        return mapping[ct]
    if ct.startswith("image/"):
        return f"картинка ({ct.split('/')[1]})"
    if ct.startswith("video/"):
        return f"видео ({ct.split('/')[1]})"
    if ct.startswith("audio/"):
        return f"аудио ({ct.split('/')[1]})"
    return ct


async def _tls_info(host: str, port: int = 443) -> dict | None:
    try:
        loop = asyncio.get_running_loop()
        ctx = ssl.create_default_context()
        def _grab():
            with socket.create_connection((host, port), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    return ssock.getpeercert()
        cert = await loop.run_in_executor(None, _grab)
        if not cert:
            return None
        issuer = dict(x[0] for x in cert.get("issuer", []))
        return {
            "issuer": issuer.get("organizationName") or issuer.get("commonName") or "—",
            "not_after": cert.get("notAfter", "—"),
        }
    except Exception:
        return None


def _score(redirects: list[str], status: int | None, host: str, tls_ok: bool, error: str | None) -> tuple[str, list[str]]:
    notes: list[str] = []
    risk = 0

    if error:
        notes.append(f"ошибка: {error}")
        risk += 3

    if status is not None:
        if 200 <= status < 300:
            pass
        elif 300 <= status < 400:
            notes.append(f"редирект {status}")
        elif 400 <= status < 500:
            notes.append(f"клиентская ошибка {status}")
            risk += 1
        elif status >= 500:
            notes.append(f"сервер падает {status}")
            risk += 1

    if len(redirects) > 3:
        notes.append(f"много редиректов ({len(redirects) - 1})")
        risk += 1

    if host:
        tld = host.rsplit(".", 1)[-1]
        if tld in SUSPICIOUS_TLDS:
            notes.append(f"подозрительный TLD .{tld}")
            risk += 2
        if host in SHORTENERS:
            notes.append("сокращалка")
            risk += 1
        if host.count(".") >= 4:
            notes.append("много субдоменов")
            risk += 1
        if any(c in host for c in "0123456789") and re.search(r"\d{2,}", host):
            notes.append("цифры в домене")

    if not tls_ok and redirects and redirects[-1].startswith("https"):
        notes.append("TLS-сертификат не проверился")
        risk += 1

    if redirects:
        start_host = _hostname(redirects[0])
        end_host = _hostname(redirects[-1])
        if start_host and end_host and start_host != end_host:
            notes.append(f"итоговый хост: {end_host}")

    if risk == 0:
        verdict = "✅ выглядит безопасно"
    elif risk <= 2:
        verdict = "⚠️ с оговорками"
    else:
        verdict = "❌ подозрительно"
    return verdict, notes


async def check(url: str) -> str:
    try:
        url = _normalize(url)
    except UnsafeURL as exc:
        return f"<b>Slim bot | Link</b>\n<blockquote>[x] {esc(str(exc))}</blockquote>"
    redirects: list[str] = [url]
    status: int | None = None
    final_url = url
    content_type = ""
    content_length: int | None = None
    server = ""
    elapsed_ms = 0
    error: str | None = None
    tls: dict | None = None

    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(
            timeout=TIMEOUT,
            headers={"User-Agent": UA},
        ) as sess:
            current = url
            for _ in range(MAX_REDIRECTS):
                await validate_public_url(current)
                try:
                    async with sess.head(current, allow_redirects=False, ssl=False) as r:
                        if r.status == 405 or r.status == 501:
                            raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status)
                        status = r.status
                        if r.status in (301, 302, 303, 307, 308):
                            loc = r.headers.get("Location")
                            if not loc:
                                break
                            current = str(r.url.join(aiohttp.client.URL(loc)))
                            redirects.append(current)
                            continue
                        content_type = r.headers.get("Content-Type", "")
                        cl = r.headers.get("Content-Length")
                        content_length = int(cl) if cl and cl.isdigit() else None
                        server = r.headers.get("Server", "")
                        final_url = current
                        break
                except aiohttp.ClientResponseError:
                    async with sess.get(current, allow_redirects=False, ssl=False) as r:
                        status = r.status
                        if r.status in (301, 302, 303, 307, 308):
                            loc = r.headers.get("Location")
                            if not loc:
                                break
                            current = str(r.url.join(aiohttp.client.URL(loc)))
                            redirects.append(current)
                            continue
                        content_type = r.headers.get("Content-Type", "")
                        cl = r.headers.get("Content-Length")
                        content_length = int(cl) if cl and cl.isdigit() else None
                        server = r.headers.get("Server", "")
                        final_url = current
                        break
            else:
                error = "слишком много редиректов"
    except asyncio.TimeoutError:
        error = "таймаут"
    except UnsafeURL as e:
        error = str(e)
    except aiohttp.ClientConnectorError as e:
        error = f"не подключиться: {e.os_error or e}"
    except aiohttp.ClientError as e:
        error = f"{type(e).__name__}: {e}"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    host = _hostname(final_url)
    if final_url.startswith("https://") and host:
        tls = await _tls_info(host)

    verdict, notes = _score(redirects, status, host, tls is not None or not final_url.startswith("https://"), error)

    parts = [f"<b>🔗 Проверка ссылки</b>"]
    parts.append(f"URL: <code>{esc(url)}</code>")
    if final_url != url:
        parts.append(f"Итог: <code>{esc(final_url)}</code>")
    parts.append(f"Хост: <code>{esc(host or '—')}</code>")
    parts.append(f"Статус: <code>{status if status is not None else '—'}</code>")
    parts.append(f"Тип: {esc(_kind(content_type))}")
    if content_length is not None:
        kb = content_length / 1024
        parts.append(f"Размер: <code>{kb:.1f} KB</code>" if kb < 1024 else f"Размер: <code>{kb / 1024:.2f} MB</code>")
    if server:
        parts.append(f"Сервер: <code>{esc(server[:50])}</code>")
    if tls:
        parts.append(f"TLS: <code>{esc(tls['issuer'])}</code> до <code>{esc(tls['not_after'])}</code>")
    if len(redirects) > 1:
        parts.append(f"Редиректы ({len(redirects) - 1}):")
        for i, u in enumerate(redirects):
            arrow = "└─" if i == len(redirects) - 1 else "├─"
            parts.append(f"<code>{arrow} {esc(u[:80])}</code>")
    parts.append(f"Время: <code>{elapsed_ms}ms</code>")
    parts.append("")
    parts.append(f"<b>Вердикт:</b> {verdict}")
    if notes:
        for n in notes:
            parts.append(f"  • {esc(n)}")

    return "\n".join(parts)


URL_RE = re.compile(
    r"(?:https?://(?:\[[0-9a-f:]+\]|[^\s<>]+)|"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}"
    r"(?::\d{1,5})?(?:/[^\s<>]*)?)",
    re.IGNORECASE,
)


def extract_url(args: str, reply_text: str | None = None) -> str | None:
    if args:
        m = URL_RE.search(args)
        if m:
            return m.group(0).rstrip(".,;:!?)]}")
    if reply_text:
        m = URL_RE.search(reply_text)
        if m:
            return m.group(0).rstrip(".,;:!?)]}")
    return None
