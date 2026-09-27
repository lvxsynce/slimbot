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

QUOTE_CMDS = (".quote", ".цитата", ".q", ".цит")

QUOTE_VIDEO_MAX_BYTES = 20 * 1024 * 1024

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


def _build_caption(first: str, last: str, fallback_name: str, sender_id, usernames: list, date_str: str) -> str:
    """Caption под файл цитаты: имя + фамилия + ID + username + дата. Pure.

    Telegram режет caption на 1024 символах (иначе BadRequest 'can't parse') —
    кап с запасом: юзернеймов максимум 5, итог максимум 1000 символов.
    """
    name = ((first or "") + " " + (last or "")).strip() or (fallback_name or "?")
    lines = [f"<b>{_esc(name)}</b>"]
    if sender_id:
        lines.append(f"ID: <code>{_esc(sender_id)}</code>")
    if usernames:
        shown = list(usernames)[:5]
        lines.append("Username: " + " ".join(f"@{_esc(u)}" for u in shown))
        if len(usernames) > 5:
            lines.append(f"<i>…и ещё {len(usernames) - 5}</i>")
    if date_str and date_str != "—":
        lines.append(_esc(date_str))
    out = "\n".join(lines)
    if len(out) > 1000:
        out = out[:1000].rsplit("\n", 1)[0]
    return out


_ANIMATED_STICKER_MIMES = {"application/x-tgsticker", "application/x-tgs-sticker"}


def _reply_media_kind(reply) -> str | None:
    """Классификация media реплая для .q: 'video' | 'photo' | None.

    - video: video / video_note / animation (GIF) / документ с video-mime.
    - photo: photo / документ с image-mime (включая статичные стикеры — нет,
      стикеры отклоняем: это не фото).
    Анимированные стикеры (TGS) и всё остальное → None.
    """
    if reply is None:
        return None
    doc = getattr(reply, "document", None)
    mime = (getattr(doc, "mime_type", "") or "") if doc else ""
    if mime in _ANIMATED_STICKER_MIMES:
        return None
    if getattr(reply, "sticker", None):
        return None
    if (
        getattr(reply, "video", None)
        or getattr(reply, "video_note", None)
        or getattr(reply, "animation", None)
        or getattr(reply, "gif", None)
        or mime.startswith("video/")
    ):
        return "video"
    if getattr(reply, "photo", None) or mime.startswith("image/"):
        return "photo"
    return None


def _voice_duration(reply) -> int | None:
    """Длительность голосового из атрибутов документа. Pure-ish, без сети."""
    voice = getattr(reply, "voice", None)
    if not voice:
        return None
    for attr in getattr(voice, "attributes", None) or []:
        duration = getattr(attr, "duration", None)
        if isinstance(duration, (int, float)) and duration > 0:
            return int(duration)
    return None


def _voice_body(reply) -> str | None:
    """Текст-заглушка для цитаты голосового. None если это не войс."""
    if not getattr(reply, "voice", None):
        return None
    duration = _voice_duration(reply)
    if duration:
        minutes, seconds = divmod(duration, 60)
        length = f"{minutes}:{seconds:02d}" if minutes else f"{seconds} сек"
        return f"🎤 Голосовое сообщение ({length})"
    return "🎤 Голосовое сообщение"


async def _render_info_strip(body_text, sender_name, sender_id, usernames_list, avatar_bytes, date_str):
    """Info-плашка (слева от GIF-кадров) в executor'е. None если недоступно."""
    try:
        from utils.quote_image import render_info_strip_png, is_available as pillow_ok
        if not pillow_ok():
            return None
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: render_info_strip_png(
                sender_name=sender_name,
                sender_id=sender_id,
                usernames=usernames_list,
                avatar_bytes=avatar_bytes,
                timestamp=date_str,
                body=body_text,
            ),
        )
    except Exception as e:
        logger.warning(f"quote: info strip render failed: {type(e).__name__}: {e}")
        return None


async def _render_card_png(body_text, sender_name, sender_id, usernames_list, avatar_bytes, date_str, background_bytes=None):
    """PNG-карточка в executor'е (CPU-heavy Pillow). None если недоступно."""
    try:
        from utils.quote_image import render_quote_png, is_available as pillow_ok
        if not pillow_ok():
            return None
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: render_quote_png(
                body=body_text,
                sender_name=sender_name,
                sender_id=sender_id,
                usernames=usernames_list,
                avatar_bytes=avatar_bytes,
                timestamp=date_str,
                background_bytes=background_bytes,
            ),
        )
    except Exception as e:
        logger.warning(f"quote: card render failed, will try fallback: {type(e).__name__}: {e}")
        return None


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
    sender_first = ""
    sender_last = ""
    sender_id = None
    if sender is not None:
        try:
            uname = getattr(sender, "username", None)
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            sender_first, sender_last = first, last
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

    # Body — text or caption; голосовое без текста — заглушка с длительностью
    body = (getattr(reply, "raw_text", "") or getattr(reply, "message", "") or "").strip()
    if not body:
        body = (getattr(reply, "caption", "") or "").strip()
    voice_attach = False
    if not body:
        voice_body = _voice_body(reply)
        if voice_body:
            body = voice_body
            voice_attach = True
    if not body and not voice_attach:
        # Без текста цитируем само сообщение: карточка-шапка без bubble
        # всегда уходит фоткой (фото/видео из реплая — фоном/GIF-кой ниже).
        # Ветки «цитировать нечего» больше нет: любой реплай → фото.
        body = ""

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

    # ---- Без caption: вся информация уже в картинке (имя/ID/username/дата
    # в шапке). Цитата уходит только фоткой, без текста под ней. ----
    # _build_caption оставлен для совместимости (тесты), но не используется.

    # ---- Классификация media реплая: фон (фото) / GIF-путь (видео, incl. кружки) / войс ----
    background_bytes: bytes | None = None
    video_bytes: bytes | None = None
    voice_bytes: bytes | None = None
    try:
        media_kind = _reply_media_kind(reply)
        if media_kind == "video":
            size = None
            for holder in (getattr(reply, "document", None), getattr(reply, "video", None),
                           getattr(reply, "video_note", None), getattr(reply, "animation", None)):
                size = getattr(holder, "size", None)
                if size:
                    break
            if not size or size <= QUOTE_VIDEO_MAX_BYTES:
                video_bytes = await reply.download_media(file=bytes)
        elif media_kind == "photo":
            background_bytes = await reply.download_media(file=bytes)
        if voice_attach:
            vsize = getattr(getattr(reply, "voice", None), "size", None)
            if not vsize or vsize <= QUOTE_VIDEO_MAX_BYTES:
                voice_bytes = await reply.download_media(file=bytes)
    except Exception as e:
        logger.debug(f"quote: reply media download failed: {e}")

    # ---- Попытка 0: GIF-цитата (видео/анимация/GIF в реплае) ----
    # Слева info-плашка (инфо + подпись), справа кадры из реплая.
    if video_bytes:
        png_card = await _render_info_strip(
            body_text, sender_name, sender_id, usernames_list, avatar_bytes, date_str
        )
        if png_card:
            try:
                from utils.quote_image import render_video_quote_gif
                gif_bytes = await render_video_quote_gif(video_bytes, card_png_bytes=png_card)
            except Exception as e:
                logger.debug(f"quote: video GIF render failed: {e}")
                gif_bytes = None
            if gif_bytes:
                try:
                    from telethon.tl.types import DocumentAttributeAnimated
                    from utils.telethon_manager import telethon_reply_to
                    import io
                    buf = io.BytesIO(gif_bytes)
                    buf.name = "quote.gif"
                    await event.client.send_file(
                        event.chat_id,
                        file=buf,
                        reply_to=telethon_reply_to(event),
                        force_document=False,
                        attributes=[DocumentAttributeAnimated()],
                        mime_type="image/gif",
                    )
                    try:
                        await event.delete()
                    except Exception as e:
                        logger.debug(f"quote: delete origin failed: {e}")
                    return
                except Exception as e:
                    logger.warning(f"quote: GIF send failed, falling back to PNG: {e}")

    # ---- Попытка 1: PNG render + send_file ----
    sent = False
    png_bytes = await _render_card_png(
        body_text, sender_name, sender_id, usernames_list,
        avatar_bytes, date_str, background_bytes,
    )
    if png_bytes:
        try:
            from utils.telethon_manager import telethon_reply_to
            import io
            buf = io.BytesIO(png_bytes)
            buf.name = "quote.png"
            quote_msg = await event.client.send_file(
                event.chat_id,
                file=buf,
                reply_to=telethon_reply_to(event),
                force_document=False,
            )
            sent = True
            # Цитата с голосовым: прикладываем оригинальный войс ответом на цитату.
            if voice_bytes:
                try:
                    import io as _io
                    vbuf = _io.BytesIO(voice_bytes)
                    vbuf.name = "voice.ogg"
                    quote_id = getattr(quote_msg, "id", None)
                    await event.client.send_file(
                        event.chat_id,
                        file=vbuf,
                        reply_to=quote_id,
                        voice_note=True,
                    )
                except Exception as e:
                    logger.debug(f"quote: voice attach failed: {e}")
        except Exception as e:
            logger.warning(f"quote: PNG send failed, falling back to HTML: {e}")

    # ---- Order-fix: delete только при sent=True ----
    if sent:
        try:
            await event.delete()
        except Exception as e:
            logger.debug(f"quote: delete origin failed: {e}")
    else:
        # PNG fail → visible fallback HTML blockquote
        await event.edit(fallback_html, parse_mode="html")
