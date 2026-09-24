"""Unified network analysis plus hash, UUID and base64 utilities."""

import asyncio
import ipaddress
from html import escape as _h

from aiogram import Router, types

from ._base import command_card, dispatch, thread_kwargs
from utils.escape import esc
from utils.hashing import ALGOS, b64_op, hash_bytes, hash_text, gen_uuids
from utils.linkcheck import check as check_link, extract_url
from utils.netinfo import dns_lookup, ip_info, passive_related_ips, unshorten
from utils.texts import Texts, render_for_user

router = Router()
NET_CMDS = (".net", ".сеть", ".сет")
HASH_CMDS = (".hash", ".хеш", ".хэш")
UUID_CMDS = (".uuid", ".юид")
B64_CMDS = (".b64", ".base64")


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
        return command_card("Net", "\n".join(lines))

    from urllib.parse import urlsplit
    parsed = urlsplit(clean if "://" in clean else "//" + clean)
    host = parsed.hostname or ""
    if not host or len(host) > 253:
        return command_card("Net", "Некорректный домен или URL.")

    dns_results = await asyncio.gather(
        *(dns_lookup(host, record_type) for record_type in ("A", "AAAA", "MX", "NS")),
        return_exceptions=True,
    )
    lines = [f"Тип: {'URL' if kind == 'url' else 'домен'}", f"Цель: <code>{esc(clean)}</code>", f"Хост: <code>{esc(host)}</code>"]
    if kind == "url":
        link_result = (await check_link(clean)).replace("<b>🔗 Проверка ссылки</b>", "Проверка URL")
        link_result = link_result.replace("✅", "").replace("⚠️", "").replace("❌", "")
        if link_result.startswith("<blockquote>") and link_result.endswith("</blockquote>"):
            link_result = link_result[len("<blockquote>"):-len("</blockquote>")]
        lines.append(link_result.strip())
        redirects = await unshorten(clean)
        if isinstance(redirects, list) and len(redirects) > 1:
            lines.append("Редиректы: " + " → ".join(f"<code>{esc(item[:80])}</code>" for item in redirects))
    for record_type, result in zip(("A", "AAAA", "MX", "NS"), dns_results):
        if isinstance(result, list) and result:
            lines.append(f"{record_type}: " + ", ".join(f"<code>{esc(value)}</code>" for value in result[:4]))

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
    await dispatch(message, await _do_hash(await resolve_effective_uid(message), _args(message.text), message))


async def _do_uuid(uid, args: str) -> str:
    n = max(1, min(int(args) if args.strip().lstrip("+-").isdigit() else 1, 20))
    body = "\n".join(f"<code>{value}</code>" for value in gen_uuids(n))
    return command_card("UUID", f"UUID4 x{n}\n{body}")


@router.message(lambda message: _is(UUID_CMDS, message.text))
async def cmd_uuid_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    await dispatch(message, await _do_uuid(await resolve_effective_uid(message), _args(message.text)))


async def _do_b64(uid, args: str, reply_text: str | None) -> str:
    parts = args.split(maxsplit=1) if args else []
    mode = parts[0].lower() if parts and parts[0].lower() in {"encode", "decode", "e", "d", "enc", "dec"} else "encode"
    if mode in {"e", "enc"}: mode = "encode"
    if mode in {"d", "dec"}: mode = "decode"
    text = parts[1] if mode in {"encode", "decode"} and len(parts) > 1 else (args or reply_text or "")
    if args and mode == "encode" and parts and parts[0].lower() in {"encode", "e", "enc"}:
        text = parts[1] if len(parts) > 1 else ""
    if not text:
        return command_card("Base64", Texts.B64.HELP.render(premium=False))
    result = b64_op(text, mode)
    return command_card("Base64", f"<i>{mode}</i>\n<code>{esc(result)}</code>")


@router.message(lambda message: _is(B64_CMDS, message.text))
async def cmd_b64_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    reply = (message.reply_to_message.text or message.reply_to_message.caption) if message.reply_to_message else None
    await dispatch(message, await _do_b64(await resolve_effective_uid(message), _args(message.text), reply))
