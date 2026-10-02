"""Unified network analysis plus hash, UUID and base64 utilities."""

import asyncio
import ipaddress
from html import escape as _h

from aiogram import Router, types

from ._base import command_card, dispatch, thread_kwargs
from utils import rate_limit_gate as gate
from utils.shared_cmd import b64_body, parse_b64_args
from utils.escape import esc
from utils.hashing import ALGOS, b64_op, hash_bytes, hash_text, gen_uuids
from utils.linkcheck import check as check_link, extract_url
from utils.netinfo import dns_lookup, ip_info, passive_related_ips, unshorten, dns_multi, http_probe, fetch_title
from utils.texts import Texts, render_for_user

router = Router()
NET_CMDS = (".net", ".сеть", ".сет")
HASH_CMDS = (".hash", ".хеш", ".хэш")
UUID_CMDS = (".uuid", ".юид")
B64_CMDS = (".b64", ".base64")

RATE_LIMITED = gate.RATE_LIMIT_TEXT


def _rate_limited(uid: str, head: str) -> bool:
    """Лимит через единый гейт (см. utils/rate_limit_gate).

    Один `.net` — это ~25 исходящих запросов (DNS к 4 резолверам, TLS-проба,
    до 10 редиректов, unshorten на 15 хопов, crt.sh + до 30 имён). Раньше в
    этом пути лимита не было вообще.
    """
    return gate.check(head, uid)


def _head(text: str | None) -> str:
    return (text or "").strip().split(maxsplit=1)[0].lower()


def _args(text: str | None) -> str:
    parts = (text or "").strip().split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _is(commands, text: str | None) -> bool:
    return _head(text) in commands


async def _do_net(uid: str, args: str, reply_text: str | None = None) -> str:
    target = (args or "").strip() or extract_url("", reply_text)
    if not target:
        return command_card("Net", "Использование: <code>.net домен, IP или URL</code>")
    clean = target.strip("<>\"'").rstrip(".,;:!?)]}")
    try:
        ipaddress.ip_address(clean)
        kind = "ip"
    except ValueError:
        kind = "url" if "://" in clean or "/" in clean else "domain"

    if kind == "ip":
        info = await ip_info(clean)
        lines = [f"Тип: IP", f"Адрес: <code>{esc(clean)}</code>"]
        if info.get("error"):
            lines.append(f"Ошибка: {esc(info['error'])}")
        else:
            for key, label in (("city", "Город"), ("region", "Регион"), ("country", "Страна"), ("org", "ASN/провайдер"), ("timezone", "TZ")):
                if info.get(key):
                    lines.append(f"{label}: <code>{esc(info[key])}</code>")
        try:
            ptr = await dns_lookup(ipaddress.ip_address(clean).reverse_pointer, "PTR")
            if isinstance(ptr, list) and ptr:
                lines.append(f"PTR: <code>{esc(ptr[0].rstrip('.'))}</code>")
        except ValueError:
            pass
        return command_card("Net", "\n".join(lines))

    from urllib.parse import urlsplit
    parsed = urlsplit(clean if "://" in clean else "//" + clean)
    host = parsed.hostname or ""
    if not host or len(host) > 253:
        return command_card("Net", "Некорректный домен или URL.")

    dns_results = await asyncio.gather(
        *(dns_lookup(host, record_type) for record_type in ("A", "AAAA", "MX", "NS")),
        dns_multi(host),
        http_probe(host),
        return_exceptions=True,
    )
    *dns_only, multi_res, probe_res = dns_results
    lines = [f"Тип: {'URL' if kind == 'url' else 'домен'}", f"Цель: <code>{esc(clean)}</code>", f"Хост: <code>{esc(host)}</code>"]
    if kind == "url":
        link_result, title_info = await asyncio.gather(check_link(clean), fetch_title(clean))
        link_result = (link_result).replace("<b>🔗 Проверка ссылки</b>", "Проверка URL")
        link_result = link_result.replace("✅", "").replace("⚠️", "").replace("❌", "")
        if link_result.startswith("<blockquote>") and link_result.endswith("</blockquote>"):
            link_result = link_result[len("<blockquote>"):-len("</blockquote>")]
        lines.append(link_result.strip())
        if isinstance(title_info, dict):
            if title_info.get("title"):
                lines.append(f"Title: {esc(title_info['title'])}")
            elif title_info.get("error"):
                lines.append(f"Title: — ({esc(title_info['error'])})")
        redirects = await unshorten(clean)
        if isinstance(redirects, list) and len(redirects) > 1:
            lines.append("Редиректы: " + " → ".join(f"<code>{esc(item[:80])}</code>" for item in redirects))
    for record_type, result in zip(("A", "AAAA", "MX", "NS"), dns_only):
        if isinstance(result, list) and result:
            lines.append(f"{record_type}: " + ", ".join(f"<code>{esc(value)}</code>" for value in result[:4]))

    if isinstance(multi_res, dict) and multi_res:
        lines.append("<b>DNS у резолверов:</b>")
        for label, res in multi_res.items():
            ips = res.get("ips") or []
            if ips:
                lines.append(f"• {esc(label)} <i>{res.get('ms', 0)}ms</i>: " + ", ".join(f"<code>{esc(ip)}</code>" for ip in ips[:3]))
            else:
                lines.append(f"• {esc(label)}: — ({esc(res.get('error') or 'нет данных')})")
    if isinstance(probe_res, dict):
        if probe_res.get("status") is not None:
            srv = f" · {esc(probe_res['server'][:30])}" if probe_res.get("server") else ""
            lines.append(f"Сайт: <code>{esc(probe_res.get('url', ''))}</code> → <code>{probe_res['status']}</code> <i>{probe_res.get('ms', 0)}ms</i>{srv}")
        else:
            lines.append(f"Сайт: недоступен ({esc(probe_res.get('error') or 'нет данных')})")

    passive = await passive_related_ips(host)
    current = set(passive.get("current_ips", []))
    candidates = [item for item in passive.get("candidates", []) if item["ip"] not in current]
    lines.append("<b>Пассивный аудит раскрытия origin</b>")
    if candidates:
        lines.append("Возможные связанные IP, не подтверждённые как origin:")
        for item in candidates[:8]:
            lines.append(f"<code>{esc(item['ip'])}</code> · {esc(', '.join(item['hosts']))} · источник: {esc(', '.join(item['sources']))}")
    else:
        lines.append("Публичных связанных IP, отличных от текущих DNS-адресов, не найдено.")
    if passive.get("errors"):
        lines.append("<i>Часть источников недоступна: " + ", ".join(esc(error) for error in passive["errors"]) + "</i>")
    return command_card("Net", "\n".join(lines))


@router.message(lambda message: _is(NET_CMDS, message.text))
async def cmd_net_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    if not _rate_limited(uid, ".net"):
        await dispatch(message, command_card("Net", RATE_LIMITED))
        return
    reply = message.reply_to_message.text if message.reply_to_message else None
    await dispatch(message, await _do_net(uid, _args(message.text), reply))


async def _download_reply_bytes(message: types.Message) -> tuple[bytes | None, str | None]:
    reply = message.reply_to_message
    if not reply:
        return None, None
    file_id = None
    hint = None
    if reply.document:
        file_id, hint = reply.document.file_id, reply.document.file_name or "документ"
    elif reply.photo:
        file_id, hint = reply.photo[-1].file_id, "фото"
    elif reply.video:
        file_id, hint = reply.video.file_id, "видео"
    elif reply.audio:
        file_id, hint = reply.audio.file_id, reply.audio.title or reply.audio.file_name or "аудио"
    elif reply.voice:
        file_id, hint = reply.voice.file_id, "голосовое"
    elif reply.video_note:
        file_id, hint = reply.video_note.file_id, "кружочек"
    elif reply.sticker and not (reply.sticker.is_animated or reply.sticker.is_video):
        file_id, hint = reply.sticker.file_id, "стикер"
    if not file_id:
        return None, None
    try:
        downloaded = await message.bot.download(file_id)
        if downloaded:
            downloaded.seek(0)
            return downloaded.read(), hint
    except Exception:
        pass
    return None, hint


async def _do_hash(uid, args: str, message: types.Message) -> str:
    reply_text = (message.reply_to_message.text or message.reply_to_message.caption) if message.reply_to_message else ""
    file_data, file_hint = await _download_reply_bytes(message)
    parts = args.split(maxsplit=1) if args else []
    algo = parts[0].lower() if parts and parts[0].lower() in ALGOS else "sha256"
    text = parts[1] if parts and parts[0].lower() in ALGOS and len(parts) > 1 else (args or reply_text)
    if file_data is not None:
        result = hash_bytes(algo, file_data)
        return command_card("Hash", f"<b>{algo}</b>\n<i>{esc(file_hint or 'файл')} · {len(file_data)} байт</i>\n<code>{esc(result)}</code>")
    if not text:
        if file_hint:
            return command_card("Hash", f"Не удалось скачать {esc(file_hint)}.")
        return await render_for_user(uid, Texts.Hash.HELP, algos=", ".join(ALGOS))
    result = hash_text(algo, text)
    if isinstance(result, str) and result.startswith("[x]"):
        return command_card("Hash", esc(result))
    return command_card("Hash", f"<b>{algo}</b>\n<i>{esc(text[:50])}</i>\n<code>{esc(result)}</code>")


@router.message(lambda message: _is(HASH_CMDS, message.text))
async def cmd_hash_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    if not _rate_limited(uid, ".hash"):
        await dispatch(message, command_card("Hash", RATE_LIMITED), card_title=None)
        return
    await dispatch(message, await _do_hash(uid, _args(message.text), message))


async def _do_uuid(uid, args: str) -> str:
    n = max(1, min(int(args) if args.strip().lstrip("+-").isdigit() else 1, 20))
    body = "\n".join(f"<code>{value}</code>" for value in gen_uuids(n))
    return command_card("UUID", f"UUID4 x{n}\n{body}")


@router.message(lambda message: _is(UUID_CMDS, message.text))
async def cmd_uuid_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    if not _rate_limited(uid, ".uuid"):
        await dispatch(message, command_card("UUID", RATE_LIMITED))
        return
    await dispatch(message, await _do_uuid(uid, _args(message.text)))


async def _do_b64(uid, args: str, reply_text: str | None) -> str:
    """Единая реализация `.b64` (utils/shared_cmd) для обоих путей.

    Раньше здесь была вторая копия: без модификатора `url` и с другой
    разметкой, чем в Telethon-пути, — одна команда вела себя по-разному
    в зависимости от места вызова.
    """
    mode, text, url_safe = parse_b64_args(args)
    if not text:
        text = reply_text or ""
    if not text:
        return command_card("Base64", Texts.B64.HELP.render(premium=False))
    result = b64_op(text, mode, url_safe=url_safe)
    if isinstance(result, str) and result.startswith("[x]"):
        return command_card("Base64", esc(result))
    return command_card("Base64", b64_body(mode, text, result, url_safe))


@router.message(lambda message: _is(B64_CMDS, message.text))
async def cmd_b64_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    if not _rate_limited(uid, ".b64"):
        await dispatch(message, command_card("Base64", RATE_LIMITED))
        return
    reply = (message.reply_to_message.text or message.reply_to_message.caption) if message.reply_to_message else None
    await dispatch(message, await _do_b64(uid, _args(message.text), reply))
