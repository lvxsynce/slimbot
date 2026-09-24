"""Команда .quote / .цитата — красиво оформить replied сообщение В ВИДЕ КАРТИНКИ (v2).

Telethon-only потому что нужны:
- точная дата сообщения (event.message.date + timezone)
- аватарка отправителя (Telethon `download_profile_photo` → bytes)
- multi-username (Collectible usernames Premium + legacy entity.username)
- full chat context (event.chat.title)

PNG rendering v2 реализован в utils/quote_image.py (PIL/Pillow + DejaVu Sans).
Layout: прямоугольный, avatar слева вверху, semi-transparent bubble-overlay
под body текстом, info-строка с ID + @usernames + timestamp.

Если Pillow/font недоступны или render fails → graceful fallback на HTML.
"""
import asyncio
import html as _html
import logging

QUOTE_CMDS = (".quote", ".цитата")

logger = logging.getLogger(__name__)

from ._base import command_card


def _esc(s, *, quote: bool = False) -> str:
    """HTML-escape для user-controlled полей (Telethon ответы)."""
    if s is None:
        return ""
    return _html.escape(str(s), quote=quote)


def _truncate(text: str, n: int = 2000) -> str:
    """Truncate raw text до n chars + '…' suffix."""
    if not text:
        return ""
    if len(text) <= n:
        return text
    return text[:n] + "…"


async def handle(user_id: str, event) -> None:
    """Telethon-вызов из telethon_manager._handle_outgoing."""
    reply = await event.get_reply_message()
    if not reply:
        await event.edit(
            "<b>Slim bot | Quote</b>\n<blockquote>[?] Ответь этой командой на сообщение, "
            "которое хочешь оформить как цитату.</blockquote>",
            parse_mode="html",
        )
        return

    sender = await reply.get_sender()
    sender_name = "?"
    sender_id = None
    if sender is not None:
        try:
            uname = getattr(sender, "username", None)
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            sender_name = (first + " " + last).strip() or uname or str(getattr(sender, "id", "?"))
            sender_id = getattr(sender, "id", None)
        except Exception:
            pass

    # Forwarded-from info (если есть) — для HTML-fallback
    fwd_text = ""
    if getattr(reply, "fwd_from", None):
        fwd = reply.fwd_from
        from_name = getattr(fwd, "from_name", None)
        from_id = getattr(fwd, "from_id", None)
        if from_name:
            fwd_text = f" · ↪ {_esc(from_name)}"
        elif from_id:
            fwd_text = f" · ↪ <code>{from_id}</code>"

    # Date
    date_str = "—"
    if getattr(reply, "date", None):
        try:
            date_str = reply.date.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

    # Body — text or caption
    body = (getattr(reply, "raw_text", "") or getattr(reply, "message", "") or "").strip()
    if not body:
        body = (getattr(reply, "caption", "") or "").strip()
    if not body:
        await event.edit(
            "<b>Slim bot | Quote</b>\n<blockquote>[?] Сообщение без текста (медиа-only). "
            "Цитировать нечего.</blockquote>",
            parse_mode="html",
        )
        return

    body_text = _truncate(body)

    # ---- Fetch avatar + usernames через TelethonManager ----
    avatar_bytes: bytes | None = None
    usernames_list: list[str] = []
    if sender is not None and sender_id:
        try:
            from utils.telethon_manager import telethon_manager
            photo = await telethon_manager.download_avatar_bytes(user_id, sender)
            if photo:
                _, photo_data = photo
                avatar_bytes = photo_data
        except Exception as e:
            logger.debug(f"quote: download_avatar_bytes failed for {sender_id}: {e}")

        try:
            from telethon.tl.functions.users import GetFullUserRequest
            from telethon.tl.types import User as TUser

            client = getattr(event, "client", None)
            if client is not None and isinstance(sender, TUser):
                full_user = await asyncio.wait_for(
                    client(GetFullUserRequest(sender.id)),
                    timeout=10,
                )
                # Вспомогательный helper в telethon_manager — корректно
                # объединяет collectible usernames + legacy entity.username.
                from utils.telethon_manager import _extract_telethon_usernames
                # Это синхронная функция — выполним в executor чтоб не блокировать loop.
                loop = asyncio.get_event_loop()
                usernames_list = await loop.run_in_executor(
                    None, _extract_telethon_usernames, sender, full_user,
                )
        except Exception as e:
            logger.debug(f"quote: username extraction failed for {sender_id}: {e}")

    # ---- HTML-fallback (используется если Pillow fail) ----
    name_html = _esc(sender_name)
    if usernames_list:
        # Multiple usernames — объединяем через ", "
        unames_html = ", ".join(f"@{_esc(u)}" for u in usernames_list)
        unames_line = f"Юзернейм: {unames_html}" if len(usernames_list) == 1 else f"Юзернеймы: {unames_html}"
    else:
        unames_line = ""
    id_line = f"ID: <code>{_esc(sender_id)}</code>" if sender_id else ""

    meta_parts = [p for p in (id_line, unames_line, _esc(date_str), fwd_text) if p]
    meta_line = " · ".join(meta_parts)

    fallback_lines = [
        "<blockquote>",
        _esc(body_text),
        "",
        f"<i>— {name_html}{(' · ' + meta_line) if meta_line else ''}</i>",
        "</blockquote>",
    ]
    fallback_html = command_card("Quote", "\n".join(fallback_lines))

    # ---- Попытка 1: PNG render + send_file ----
    sent = False
    try:
        from utils.quote_image import render_quote_png, is_available as pillow_ok
        if pillow_ok():
            # Синхронный CPU-heavy PNG-рендер (Pillow) — в executor, чтобы
            # не блокировать event loop на время отрисовки.
            png_bytes = await asyncio.get_running_loop().run_in_executor(
                None,
                render_quote_png,
                body_text,
                sender_name,
                sender_id,
                usernames_list,
                avatar_bytes,
                date_str,
            )
            if png_bytes:
                from utils.telethon_manager import telethon_reply_to
                import io
                buf = io.BytesIO(png_bytes)
                buf.name = "quote.png"
                await event.client.send_file(
                    event.chat_id,
                    file=buf,
                    reply_to=telethon_reply_to(event),
                    force_document=False,
                )
                sent = True
    except Exception as e:
        logger.warning(f"quote: PNG render/send failed, falling back to HTML: {e}")

    # ---- Order-fix: delete только при sent=True ----
    if sent:
        try:
            await event.delete()
        except Exception as e:
            logger.debug(f"quote: delete origin failed: {e}")
    else:
        # PNG fail → visible fallback HTML blockquote
        await event.edit(fallback_html, parse_mode="html")
