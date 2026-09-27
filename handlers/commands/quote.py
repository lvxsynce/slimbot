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

# PUA-диапазон для маркеров custom emoji в тексте цитаты.
_PUA_BASE = 0xE000
_PUA_COUNT = 0xF8FF - 0xE000


def _custom_emoji_spans(reply) -> list[tuple[int, int, int]]:
    """[(offset, length, document_id)] custom emoji из entities. Pure.

    Оффсеты Telegram — в UTF-16 code units (астральные эмодзи = 2 юнита).
    """
    spans = []
    for ent in getattr(reply, "entities", None) or []:
        if ent.__class__.__name__ != "MessageEntityCustomEmoji":
            continue
        doc_id = getattr(ent, "document_id", None)
        if doc_id:
            spans.append((
                getattr(ent, "offset", 0) or 0,
                getattr(ent, "length", 0) or 0,
                doc_id,
            ))
    return spans


def _apply_emoji_placeholders(text: str, spans: list[tuple[int, int, int]],
                              available: set[int] | None = None
                              ) -> tuple[str, dict[str, int]]:
    """Заменяет диапазоны custom emoji на PUA-маркеры. Pure.

    Заменяются ТОЛЬКО document_id из `available` (картинка скачалась);
    остальные оставляем исходным символом — иначе эмодзи исчезнет из
    цитаты полностью. `available=None` = заменять все (legacy для тестов).
    Возвращает (текст, {pua_char: document_id}).
    """
    if not text or not spans:
        return text, {}
    try:
        units = text.encode("utf-16-le")
    except Exception:
        return text, {}
    out = bytearray()
    mapping: dict[str, int] = {}
    idx = 0
    pos = 0
    for off, ln, doc_id in sorted(spans):
        if available is not None and doc_id not in available:
            continue
        start, end = off * 2, (off + ln) * 2
        if ln <= 0 or start < pos or end > len(units):
            continue
        out += units[pos:start]
        pua = chr(_PUA_BASE + (idx % _PUA_COUNT))
        idx += 1
        out += pua.encode("utf-16-le")
        mapping[pua] = doc_id
        pos = end
    out += units[pos:]
    try:
        return bytes(out).decode("utf-16-le"), mapping
    except Exception:
        return text, {}


def _pillow_first_frame(data: bytes):
    """Статика (webp/png/jpeg/gif) → ([RGBA], [100]). ([], []) если битое."""
    if not data:
        return [], []
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(data))
        try:
            if getattr(im, "is_animated", False):
                im.seek(0)
        except Exception:
            pass
        im.thumbnail((128, 128))
        return [im.convert("RGBA")], [100]
    except Exception:
        return [], []


async def _fetch_custom_emoji(client, doc_ids) -> dict:
    """{document_id: {'frames': [RGBA...], 'durations': [ms...]}}.

    Источники по mime:
    - application/x-tgsticker (TGS-вектор, нечем растеризовать) → статичный
      thumb с серверов Telegram (download_media thumb=-1, webp);
    - video/* (webm VP9+alpha) → все кадры через ffmpeg (для анимации);
    - остальное (webp/png/gif) → первый кадр через Pillow.
    Пустые/битые пропускаются.
    """
    res: dict = {}
    ids = [i for i in dict.fromkeys(doc_ids or []) if i]
    if not ids or client is None:
        return res
    try:
        from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
        docs = await asyncio.wait_for(client(GetCustomEmojiDocumentsRequest(document_id=ids)), timeout=15)
    except Exception as e:
        logger.debug(f"quote: custom emoji fetch failed: {e}")
        return res
    for doc in docs or []:
        doc_id = getattr(doc, "id", None)
        if not doc_id or doc_id in res:
            continue
        try:
            mime = getattr(doc, "mime_type", "") or ""
            if mime in _ANIMATED_STICKER_MIMES:
                thumb = await asyncio.wait_for(
                    client.download_media(doc, file=bytes, thumb=-1), timeout=20)
                frames, durations = _pillow_first_frame(thumb) if thumb else ([], [])
            elif mime.startswith("video/"):
                data = await asyncio.wait_for(
                    client.download_media(doc, file=bytes), timeout=20)
                if not data:
                    continue
                from utils.gif_converter import webm_emoji_to_frames
                frames, durations = await webm_emoji_to_frames(data)
                if not frames:
                    frames, durations = _pillow_first_frame(data)
            else:
                data = await asyncio.wait_for(
                    client.download_media(doc, file=bytes), timeout=20)
                frames, durations = _pillow_first_frame(data)
            if frames:
                res[doc_id] = {"frames": frames, "durations": durations or [100] * len(frames)}
        except Exception as e:
            logger.debug(f"quote: custom emoji doc {doc_id} failed: {e}")
    return res


def _select_anim_phases(n_frames: int, cap: int = 8) -> list[int]:
    """Равномерные индексы фаз анимации (≤cap). Pure."""
    if n_frames <= 0:
        return []
    k = min(cap, n_frames)
    if k == 1:
        return [0]
    return sorted({round(i * (n_frames - 1) / (k - 1)) for i in range(k)})


def _decode_emoji_frame(data: bytes, mime: str):
    """Legacy: первый кадр → PIL RGBA. Используется тестами."""
    if (mime or "") in _ANIMATED_STICKER_MIMES:
        return None
    frames, _ = _pillow_first_frame(data)
    return frames[0] if frames else None


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


def _fmt_dur(seconds) -> str | None:
    """75 → '1:15', 8 → '8 сек'. None если длительность неизвестна."""
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    s = int(seconds)
    minutes, sec = divmod(s, 60)
    return f"{minutes}:{sec:02d}" if minutes else f"{sec} сек"


def _audio_info(reply) -> tuple[bool, float | None, str]:
    """Голосовое или музыка: (есть_звук, длительность, подпись). Pure.

    Ловит и `reply.voice`, и `reply.audio`/документ с audio-mime или
    DocumentAttributeAudio (треки с названием/исполнителем).
    """
    if getattr(reply, "voice", None):
        return True, _voice_duration(reply), "🎤 Голосовое сообщение"
    doc = getattr(reply, "document", None)
    mime = (getattr(doc, "mime_type", "") or "") if doc else ""
    audio_attr = None
    for attr in getattr(doc, "attributes", None) or []:
        if attr.__class__.__name__ == "DocumentAttributeAudio":
            audio_attr = attr
            break
    if getattr(reply, "audio", None) is not None or audio_attr is not None or mime.startswith("audio/"):
        dur = getattr(audio_attr, "duration", None)
        dur = dur if isinstance(dur, (int, float)) and dur > 0 else None
        title = getattr(audio_attr, "title", None)
        perf = getattr(audio_attr, "performer", None)
        if title and perf:
            label = f"🎵 {perf} — {title}"
        elif title:
            label = f"🎵 {title}"
        else:
            label = "🎵 Аудио"
        return True, dur, label
    return False, None, ""


def _voice_body(reply) -> str | None:
    """Текст-заглушка для цитаты голосового. None если это не войс."""
    if not getattr(reply, "voice", None):
        return None
    is_audio, dur, label = _audio_info(reply)
    d = _fmt_dur(dur)
    return f"{label} ({d})" if d else label


def _scaled_video_size(card_png: bytes, max_width: int = 960) -> tuple[int, int]:
    """Размер кадра после builder-scale (min(960,iw) × чётная высота). Pure."""
    try:
        import io as _io
        from PIL import Image
        w, h = Image.open(_io.BytesIO(card_png)).size
        vw = min(max_width, w)
        vh = int(h * vw / max(w, 1)) // 2 * 2
        return max(2, vw), max(2, vh)
    except Exception:
        return 960, 540


async def _render_info_strip(body_text, sender_name, sender_id, usernames_list, avatar_bytes, date_str,
                       inline_images=None):
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
                inline_images=inline_images,
            ),
        )
    except Exception as e:
        logger.warning(f"quote: info strip render failed: {type(e).__name__}: {e}")
        return None


async def _render_card_png(body_text, sender_name, sender_id, usernames_list, avatar_bytes, date_str, background_bytes=None,
                     inline_images=None):
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
                inline_images=inline_images,
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

    # Body — text or caption. Custom (анимированные) emoji: сначала качаем
    # кадры (TGS → серверный thumb, webm → все кадры, статика → 1 кадр),
    # и ТОЛЬКО скачанные заменяем на PUA-маркеры — остальные остаются
    # исходным символом, иначе эмодзи исчезнет из цитаты полностью.
    # Замена ДО strip (оффсеты entities относятся к исходному тексту).
    raw_source = (getattr(reply, "raw_text", "") or getattr(reply, "message", "") or "")
    if not raw_source:
        raw_source = (getattr(reply, "caption", "") or "")
    spans = _custom_emoji_spans(reply)
    logger.warning(
        "quote diag: raw_len=%d entities=%s spans=%s",
        len(raw_source),
        [(e.__class__.__name__, getattr(e, "offset", None),
          getattr(e, "length", None), getattr(e, "document_id", None))
         for e in (getattr(reply, "entities", None) or [])],
        spans,
    )
    emoji_data: dict = {}
    if spans:
        try:
            emoji_data = await asyncio.wait_for(
                _fetch_custom_emoji(getattr(event, "client", None),
                                    [doc_id for _, _, doc_id in spans]),
                timeout=60,
            )
        except Exception as e:
            logger.debug(f"quote: custom emoji fetch failed: {e}")
            emoji_data = {}
        if spans and not emoji_data:
            logger.warning(f"quote: custom emoji spans={len(spans)} fetched=0 "
                           f"(uid={user_id})")
        else:
            logger.debug(f"quote: custom emoji spans={len(spans)} "
                         f"fetched={len(emoji_data)}")
    raw_source, pua_to_doc = _apply_emoji_placeholders(raw_source, spans, set(emoji_data))
    body = raw_source.strip()
    # Звук детектим независимо от текста: аудио+подпись тоже идёт в видео.
    is_audio, audio_dur, audio_label = _audio_info(reply)
    audio_attach = bool(is_audio)
    if not body and is_audio:
        d = _fmt_dur(audio_dur)
        body = f"{audio_label} ({d})" if d else audio_label
    if not body and not audio_attach:
        # Без текста цитируем само сообщение: карточка-шапка без bubble
        # всегда уходит фоткой (фото/видео из реплая — фоном/GIF-кой ниже).
        # Ветки «цитировать нечего» больше нет: любой реплай → фото.
        body = ""

    body_text = _truncate(body)

    # ---- Custom emoji → inline-картинки (статика: первый кадр) ----
    # pua → PIL Image. Анимированные (webm, >1 кадра) отдельно ниже —
    # для GIF-пути рендерим плашку по фазам.
    inline_images: dict = {}
    anim_emoji: dict = {}  # pua → entry для GIF-пути
    for ch, doc_id in pua_to_doc.items():
        if ch not in body_text:
            continue
        entry = emoji_data.get(doc_id)
        if not entry or not entry.get("frames"):
            continue
        inline_images[ch] = entry["frames"][0]
        if len(entry["frames"]) > 1:
            anim_emoji[ch] = entry

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

    # ---- Классификация media реплая: фон (фото) / GIF-путь (видео, incl. кружки) / звук ----
    background_bytes: bytes | None = None
    video_bytes: bytes | None = None
    audio_bytes: bytes | None = None
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
        if audio_attach:
            asize = None
            for holder in (getattr(reply, "voice", None), getattr(reply, "document", None),
                           getattr(reply, "audio", None)):
                asize = getattr(holder, "size", None)
                if asize:
                    break
            if not asize or asize <= QUOTE_VIDEO_MAX_BYTES:
                audio_bytes = await reply.download_media(file=bytes)
    except Exception as e:
        logger.debug(f"quote: reply media download failed: {e}")

    # ---- Попытка 0: GIF-цитата (видео/анимация/GIF в реплае) ----
    # Слева info-плашка (инфо + подпись), справа кадры из реплая.
    # Если в подписи анимированные webm-эмодзи — плашка рендерится по
    # фазам (≤8) и эмодзи движется вместе с гифкой.
    if video_bytes:
        anim_strips: list | None = None
        anim_dur_ms = 0
        if anim_emoji:
            anim_pua, anim_entry = max(
                anim_emoji.items(), key=lambda kv: len(kv[1]["frames"]))
            anim_frames = anim_entry["frames"]
            phases = _select_anim_phases(len(anim_frames))
            if len(phases) > 1:
                anim_strips = []
                for ph in phases:
                    strip = await _render_info_strip(
                        body_text, sender_name, sender_id, usernames_list,
                        avatar_bytes, date_str,
                        inline_images={**inline_images, anim_pua: anim_frames[ph]},
                    )
                    if strip:
                        anim_strips.append(strip)
                if anim_strips:
                    anim_dur_ms = sum(anim_entry.get("durations") or [100] * len(anim_frames))
                else:
                    anim_strips = None
        png_card = await _render_info_strip(
            body_text, sender_name, sender_id, usernames_list, avatar_bytes, date_str,
            inline_images=inline_images,
        )
        if png_card:
            try:
                from utils.quote_image import render_video_quote_gif
                gif_bytes = await render_video_quote_gif(
                    video_bytes, card_png_bytes=png_card,
                    anim_strips=anim_strips, anim_duration_ms=anim_dur_ms)
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

    # ---- Попытка 0.5: видео-цитата (голосовое/аудио) ----
    # Один файл: кадр-карточка (инфо + текст) + звук внутри. Отдельного
    # войса рядом больше нет — звук живёт внутри видео.
    if audio_bytes:
        audio_card = await _render_card_png(
            body_text, sender_name, sender_id, usernames_list, avatar_bytes, date_str,
            inline_images=inline_images,
        )
        if audio_card:
            try:
                from utils.gif_converter import image_audio_to_video_bytes
                clip = await image_audio_to_video_bytes(audio_card, audio_bytes)
            except Exception as e:
                logger.debug(f"quote: audio video render failed: {e}")
                clip = None
            if clip:
                try:
                    from telethon.tl.types import DocumentAttributeVideo
                    from utils.telethon_manager import telethon_reply_to
                    import io
                    vw, vh = _scaled_video_size(audio_card)
                    buf = io.BytesIO(clip)
                    buf.name = "quote.mp4"
                    await event.client.send_file(
                        event.chat_id,
                        file=buf,
                        reply_to=telethon_reply_to(event),
                        force_document=False,
                        supports_streaming=True,
                        attributes=[DocumentAttributeVideo(
                            duration=max(1, int(audio_dur or 0)),
                            w=vw,
                            h=vh,
                            round_message=False,
                            supports_streaming=True,
                        )],
                    )
                    try:
                        await event.delete()
                    except Exception as e:
                        logger.debug(f"quote: delete origin failed: {e}")
                    return
                except Exception as e:
                    logger.warning(f"quote: audio video send failed, falling back: {e}")

    # ---- Попытка 1: PNG render + send_file ----
    sent = False
    png_bytes = await _render_card_png(
        body_text, sender_name, sender_id, usernames_list,
        avatar_bytes, date_str, background_bytes,
        inline_images=inline_images,
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
            # Видео-цитата не собралась (нет ffmpeg) — прикладываем оригинал
            # звука ответом на цитату, как раньше.
            if audio_bytes:
                try:
                    import io as _io
                    is_voice = getattr(reply, "voice", None) is not None
                    mime = (getattr(getattr(reply, "document", None), "mime_type", "") or "")
                    ext = ".ogg" if is_voice else (
                        ".mp3" if mime == "audio/mpeg" else
                        ".m4a" if mime in ("audio/mp4", "audio/x-m4a") else
                        ".ogg" if mime in ("audio/ogg", "audio/opus") else ".bin")
                    vbuf = _io.BytesIO(audio_bytes)
                    vbuf.name = ("voice" if is_voice else "audio") + ext
                    quote_id = getattr(quote_msg, "id", None)
                    await event.client.send_file(
                        event.chat_id,
                        file=vbuf,
                        reply_to=quote_id,
                        voice_note=is_voice,
                    )
                except Exception as e:
                    logger.debug(f"quote: audio attach failed: {e}")
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
