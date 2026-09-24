"""Validation helpers for outbound URLs used by network tools."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeURL(ValueError):
    """The URL resolves to a local or otherwise non-public network address."""


def _is_public(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def _resolve_public(host: str, port: int) -> None:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURL("не удалось разрешить адрес") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or any(not _is_public(address) for address in addresses):
        raise UnsafeURL("адрес указывает на локальную или закрытую сеть")


async def validate_public_url(url: str) -> str:
    """Validate scheme, credentials and DNS results before an HTTP request."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURL("разрешены только HTTP и HTTPS URL")
    if parsed.username or parsed.password:
        raise UnsafeURL("URL с учётными данными запрещены")
    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        await asyncio.to_thread(_resolve_public, parsed.hostname, port)
    else:
        if not ip.is_global:
            raise UnsafeURL("адрес указывает на локальную или закрытую сеть")
    return url
