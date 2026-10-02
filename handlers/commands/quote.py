"""Команда .quote / .цитата — красиво оформить replied сообщение В ВИДЕ КАРТИНКИ (v2).

Telethon-only потому что нужны:
- точная дата сообщения (event.message.date + timezone)
- аватарка отправителя (Telethon `download_profile_photo` → bytes)
- multi-username (Collectible usernames Premium + legacy entity.username)
- full chat context (event.chat.title)

PNG rendering v2 реализован в utils/quote_image.py (PIL/Pillow + DejaVu Sans).
Layout: прямоугольный, avatar слева вверху, semi-transparent bubble-overlay
под body текстом, info-строка с ID + @usernames + timestamp.

Поддерживаемые media реплая:
- фото → фон карточки;
- видео / GIF / **кружок (video_note)** → анимированная GIF-цитата; для кружка
  дополнительно ставится круглая маска (Telegram отдаёт квадрат 200x200);
- голосовое/аудио → mp4 (кадр-карточка + AAC-звук);
- **анимированные custom emoji** (Premium) — TGS растрируется через
  rlottie-python, WEBM через ffmpeg; нескачанные остаются обычным глифом.

Если Pillow/font недоступны или render fails → graceful fallback на HTML.
"""
from utils.cmds import QUOTE_CMDS
import asyncio
import html as _html
import logging



QUOTE_VIDEO_MAX_BYTES = 20 * 1024 * 1024

logger = logging.getLogger(__name__)

from utils.escape import esc as _esc
from ._base import command_card


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
# Кастомные эмодзи — не Unicode-символы, а ссылки на документы
# (MessageEntityCustomEmoji.document_id). Чтобы отрисовать их в строку,
# диапазон entity заменяем на PUA-маркер (U+E000…), а картинку передаём
# в рендерер через `inline_images={маркер: RGBA Image}`.
_PUA_BASE = 0xE000
_PUA_COUNT = 0xF8FF - 0xE000


def _reply_media_kind(reply) -> str | None:
    """Классификация media реплая для .q.

    Возвращает: 'video' | 'video_note' | 'photo' | 'sticker' | None.

    - video: video / animation (GIF) / документ с video-mime.
    - video_note: кружок. Отдельный вид нужен только ради круглой маски —
      файл там обычный квадратный mp4 200x200, круг рисует клиент.
    - photo: photo / документ с image-mime.
    - sticker: стикер, включая анимированные (TGS) и видео (webm). Раньше
      отбрасывался целиком, из-за чего `.q` на стикере давал пустую карточку.
    """
    if reply is None:
        return None
    doc = getattr(reply, "document", None)
    mime = (getattr(doc, "mime_type", "") or "") if doc else ""
    if getattr(reply, "sticker", None):
        return "sticker"
    if getattr(reply, "video_note", None):
        # Кружок: квадратный mp4, но помечается отдельно ради circle-маски.
        return "video_note"
    if (
        getattr(reply, "video", None)
        or getattr(reply, "animation", None)
        or getattr(reply, "gif", None)
        or mime.startswith("video/")
    ):
        return "video"
    if getattr(reply, "photo", None) or mime.startswith("image/"):
        return "photo"
    if mime in _ANIMATED_STICKER_MIMES:
        # Старый формат стикеров (и часть custom-emoji) без поля .sticker.
        return "sticker"
    return None


async def _sticker_to_quote_media(data: bytes) -> tuple[bytes | None, bytes | None]:
    """Стикер → (gif_bytes | None, still_png_bytes | None).

    Анимированный стикер (TGS / webm) отдаём как GIF — он идёт в GIF-ветку
    цитаты и анимируется. Если кадров один (или нечем анимировать) —
    отдаём статичный кадр, чтобы в карточке хоть что-то было.
    """
    if not data:
        return None, None
    from utils.gif_converter import (
        frames_to_gif_bytes, rgba_frame_to_png_bytes, tgs_to_rgba_frames,
        webm_frame_durations, webm_to_rgba_frames,
    )
    frames, durations = [], []
    if data[:2] == b"\x1f\x8b":
        # TGS: rlottie (CPU-bound, десятки кадров 128px) — в поток.
        frames = await _to_thread(tgs_to_rgba_frames, data)
        if frames:
            durations = webm_frame_durations(len(frames))
    elif data[:4] in (b"RIFF", b"\x89PNG", b"\xff\xd8\xff") or data[:6] in (
        b"GIF87a", b"GIF89a",
    ):
        # Статичный webp/png/jpeg/gif-стикер — ffmpeg тут не нужен,
        # отдаём картинку как есть (фото-ветка карточки).
        return None, data
    else:
        # WEBM/MKV: ffmpeg. Альфа лежит в BlockAdditions → нужен -c:v libvpx-vp9.
        frames = await webm_to_rgba_frames(data)
        if frames:
            durations = webm_frame_durations(len(frames))
    if not frames:
        # Ничего не декодировалось — отдаём как есть (напр. статичный webp).
        return None, data
    if len(frames) > 1:
        gif = await _to_thread(frames_to_gif_bytes, frames, durations)
        if gif:
            return gif, None
    first = frames[0]
    return None, await _to_thread(rgba_frame_to_png_bytes, first)


def _custom_emoji_spans(reply) -> list[tuple[int, int, int]]:
    """[(offset, length, document_id)] custom emoji из entities. Pure.

    Оффсеты Telegram — в UTF-16 code units (астральные эмодзи = 2 юнита),
    поэтому длины считаются в юнитах, а не в символах.
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

    Заменяются ТОЛЬКО document_id из `available` (кадры скачались);
    остальные оставляем исходным символом — иначе эмодзи исчезнет из цитаты
    целиком (PUA без картинки намеренно не рисуется). `available=None` =
    заменять все (для тестов). Возвращает (текст, {pua_char: document_id}).
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


async def _to_thread(fn, *args):
    """CPU-bound (Pillow/rlottie) — в поток, иначе встаёт весь event loop.

    Один `.q` держит в RAM до 40 RGBA-кадров (bot.log: 188 КБ → 7.4 МБ),
    и вся эта работа шла прямо в корутине: Telethon-клиенты всех
    пользователей в эти секунды не могли прочитать сокет.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args))


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
    - application/x-tgsticker (TGS = gzip+Lottie) → rlottie-python растрирует
      в RGBA-кадры; если rlottie нет — статичный thumb с серверов Telegram;
    - video/* (webm VP9+alpha) → RGBA-кадры через ffmpeg (см. webm_to_rgba_frames;
      альфа лежит в Matroska BlockAdditions и требует -c:v libvpx-vp9);
    - остальное (webp/png/gif) → первый кадр через Pillow.

    Пустые/битые документы пропускаются — caller оставит исходный символ.
    """
    res: dict = {}
    ids = [i for i in dict.fromkeys(doc_ids or []) if i]
    if not ids or client is None:
        return res
    try:
        from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
        docs = await asyncio.wait_for(
            client(GetCustomEmojiDocumentsRequest(document_id=ids)), timeout=15)
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
                data = await asyncio.wait_for(
                    client.download_media(doc, file=bytes), timeout=20)
                from utils.gif_converter import tgs_to_rgba_frames
                frames = await _to_thread(tgs_to_rgba_frames, data) if data else []
                how = "rlottie"
                if not frames:
                    # rlottie недоступен / TGS битый → статичный ПРЕВЬЮ-кадр
                    # с серверов Telegram. Перебираем thumbs по индексу: для
                    # анимированных стикеров там бывает VideoSize (webm),
                    # который Pillow не открывает, а `thumb=-1` («наибольший»)
                    # может указывать ровно на него.
                    how = "thumb"
                    frames, durations = [], []
                    for idx in range(len(getattr(doc, "thumbs", None) or [])):
                        try:
                            tb = await asyncio.wait_for(
                                client.download_media(doc, file=bytes, thumb=idx),
                                timeout=20)
                        except Exception:
                            tb = None
                        if not tb:
                            continue
                        frames, durations = await _to_thread(_pillow_first_frame, tb)
                        if frames:
                            how = f"thumb[{idx}]"
                            break
                    frames = frames[:1]
                else:
                    from utils.gif_converter import webm_frame_durations
                    durations = webm_frame_durations(len(frames))
                logger.warning(
                    "quote emoji: doc=%s mime=%s tgs_bytes=%s frames=%s via=%s",
                    doc_id, mime, len(data or b""), len(frames), how,
                )
            elif mime.startswith("video/"):
                data = await asyncio.wait_for(
                    client.download_media(doc, file=bytes), timeout=20)
                if not data:
                    continue
                from utils.gif_converter import (
                    webm_frame_durations, webm_to_rgba_frames)
                frames = await webm_to_rgba_frames(data)
                if not frames:
                    frames, durations = await _to_thread(_pillow_first_frame, data), [100]
                else:
                    durations = webm_frame_durations(len(frames))
                logger.warning(
                    "quote emoji: doc=%s mime=%s webm_bytes=%s frames=%s",
                    doc_id, mime, len(data), len(frames),
                )
            else:
                data = await asyncio.wait_for(
                    client.download_media(doc, file=bytes), timeout=20)
                frames, durations = (
                    await _to_thread(_pillow_first_frame, data) if data else ([], [])
                )
                logger.warning(
                    "quote emoji: doc=%s mime=%s static_bytes=%s frames=%s",
                    doc_id, mime, len(data or b""), len(frames),
                )
            if frames:
                res[doc_id] = {"frames": frames, "durations": durations or [100] * len(frames)}
        except Exception as e:
            logger.warning(
                f"quote emoji: doc {doc_id} FAILED: {type(e).__name__}: {e}")
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
    """Legacy: первый кадр → PIL RGBA. Синхронная (используется тестами).

    Продакшн-код сюда не ходит: он уже разворачивает кадры через `_to_thread`.
    """
    if (mime or "") in _ANIMATED_STICKER_MIMES:
        return None
    frames, _ = _pillow_first_frame(data)
    return frames[0] if frames else None


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
                       inline_images=None, scale=None):
    """Info-плашка (слева от GIF-кадров) в executor'е. None если недоступно.

    ``scale`` — изотропный множитель разрешения плашки. Плашка потом
    масштабируется под высоту кадра, поэтому её выгодно рисовать с запасом:
    даунскейл текста чёткий, апскейл — каша. Масштабировать нужно обе оси,
    иначе плашка получит неверную пропорцию.
    """
    try:
        from utils.quote_image import render_info_strip_png, is_available as pillow_ok
        if not pillow_ok():
            return None
        kwargs = {}
        if scale:
            kwargs["scale"] = scale
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
                **kwargs,
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

    # Body — text or caption. Custom (анимированные) emoji — это НЕ символы
    # Unicode, а ссылки на документы (entity document_id). Сначала качаем
    # кадры (TGS → rlottie, webm → ffmpeg, статика → 1 кадр) и ТОЛЬКО
    # скачанные заменяем на PUA-маркеры: остальные оставляем исходным
    # символом, иначе эмодзи исчезнет из цитаты полностью.
    # Замена ДО strip — оффсеты entities относятся к исходному тексту.
    raw_source = (getattr(reply, "raw_text", "") or getattr(reply, "message", "") or "")
    if not raw_source:
        raw_source = (getattr(reply, "caption", "") or "")
    spans = _custom_emoji_spans(reply)
    if spans:
        # Что реально лежит в тексте на месте каждого entity: PUA-символ или
        # обычный Unicode-эмодзи (alt). От этого зависит фолбэк-стратегия.
        _probe = []
        for _off, _ln, _did in spans:
            try:
                _u = raw_source.encode("utf-16-le")
                _ch = _u[_off * 2:_off * 2 + _ln * 2].decode("utf-16-le", "replace")
                _probe.append(f"U+{ord(_ch[0]):04X}" if _ch else "empty")
            except Exception as _e:
                _probe.append(f"err:{_e}")
        logger.warning(
            "quote: spans=%d chars=%s", len(spans), ",".join(_probe),
        )
    emoji_data: dict = {}
    inline_images: dict = {}
    # {marker: (frames, total_cycle_ms)} — для анимированных фаз в GIF-цитате.
    anim_entries: dict = {}
    body = raw_source
    if spans:
        try:
            emoji_data = await asyncio.wait_for(
                _fetch_custom_emoji(getattr(event, "client", None),
                                    [doc_id for _, _, doc_id in spans]),
                timeout=60,
            )
        except Exception as e:
            logger.warning(
                f"quote: custom emoji fetch FAILED: {type(e).__name__}: {e}")
            emoji_data = {}
        # ВАЖНО: передаём именно set(emoji_data) — пустой set, а не None.
        # `None` в _apply_emoji_placeholders означает «заменить ВСЕ диапазоны»,
        # и тогда непокрытые эмодзи превратятся в PUA-маркеры без картинки →
        # `_draw_runs` их молча пропустит и эмодзи исчезнет из цитаты.
        body, marker_map = _apply_emoji_placeholders(
            raw_source, spans, set(emoji_data),
        )
        logger.warning(
            "quote: requested=%d fetched=%d replaced=%d",
            len({d for _, _, d in spans}), len(emoji_data), len(marker_map),
        )
        for marker, doc_id in marker_map.items():
            entry = emoji_data.get(doc_id) or {}
            frames = entry.get("frames") or []
            if not frames:
                continue
            inline_images[marker] = frames[0]
            if len(frames) > 1:
                durations = entry.get("durations") or []
                anim_entries[marker] = (
                    frames, sum(durations) if durations else 100 * len(frames),
                )
    body = body.strip()
    if not body:
        body = (getattr(reply, "caption", "") or "").strip()
    # Кружок без текста — подпись-заглушка (GIF-кадры идут справа).
    if not body and getattr(reply, "video_note", None):
        body = "📹 Видеосообщение"
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

    # ---- Классификация media реплая: фон (фото) / GIF-путь (видео + кружки) / звук ----
    background_bytes: bytes | None = None
    video_bytes: bytes | None = None
    audio_bytes: bytes | None = None
    is_video_note = False
    try:
        media_kind = _reply_media_kind(reply)
        if media_kind in ("video", "video_note"):
            is_video_note = media_kind == "video_note"
            size = None
            for holder in (getattr(reply, "document", None), getattr(reply, "video", None),
                           getattr(reply, "video_note", None), getattr(reply, "animation", None)):
                size = getattr(holder, "size", None)
                if size:
                    break
            if not size or size <= QUOTE_VIDEO_MAX_BYTES:
                video_bytes = await reply.download_media(file=bytes)
            logger.warning(
                "quote media: kind=%s size=%s downloaded=%s",
                media_kind, size, len(video_bytes or b"") or 0,
            )
        elif media_kind == "photo":
            background_bytes = await reply.download_media(file=bytes)
            logger.warning(
                "quote media: kind=photo downloaded=%s",
                len(background_bytes or b"") or 0,
            )
        elif media_kind == "sticker":
            # Стикер (в т.ч. анимированный TGS / видео webm): качаем и
            # превращаем либо в GIF (анимация), либо в статичный кадр.
            raw = await reply.download_media(file=bytes)
            video_bytes, background_bytes = await _sticker_to_quote_media(raw)
            logger.warning(
                "quote media: kind=sticker raw=%s gif=%s still=%s",
                len(raw or b""), len(video_bytes or b"") or 0,
                len(background_bytes or b"") or 0,
            )
        else:
            logger.warning(
                "quote media: kind=%s (no video/photo path) mime=%s "
                "video_note=%s video=%s animation=%s sticker=%s",
                media_kind, (getattr(getattr(reply, "document", None), "mime_type", "")
                             or "") if getattr(reply, "document", None) else "",
                bool(getattr(reply, "video_note", None)),
                bool(getattr(reply, "video", None)),
                bool(getattr(reply, "animation", None)),
                bool(getattr(reply, "sticker", None)),
            )
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
        logger.warning(f"quote: reply media download FAILED: {type(e).__name__}: {e}")

    # ---- Попытка 0: GIF-цитата (видео / анимация / GIF / кружок) ----
    # Слева info-плашка (инфо + подпись), справа кадры из реплая.
    if video_bytes:
        # Плашку рисуем в 2x разрешения: в `_stack_gif_side_by_side` она
        # масштабируется под высоту кадра (обычно вниз), и текст остаётся чётким.
        strip_scale = 2.0
        logger.warning(
            "quote gif: start is_video_note=%s bytes=%s",
            is_video_note, len(video_bytes),
        )
        png_card = await _render_info_strip(
            body_text, sender_name, sender_id, usernames_list, avatar_bytes, date_str,
            inline_images=inline_images, scale=strip_scale,
        )
        # Анимированный custom-emoji: для GIF-цитаты рендерим info-плашку
        # по фазам анимации, иначе эмодзи в тексте было бы статичным.
        # Анимируем только самое длинное из эмодзи (остальные — статикой):
        # иначе плашка размножится на len(эмодзи) картинок в executor'е.
        anim_strips: list | None = None
        anim_duration_ms = 0
        if anim_entries:
            marker, (frames, cycle_ms) = max(
                anim_entries.items(), key=lambda kv: len(kv[1][0]))
            phases = _select_anim_phases(len(frames))
            if len(phases) > 1:
                strips = []
                for ph in phases:
                    st = await _render_info_strip(
                        body_text, sender_name, sender_id, usernames_list,
                        avatar_bytes, date_str,
                        inline_images={**inline_images, marker: frames[ph]},
                        scale=strip_scale,
                    )
                    if st:
                        strips.append(st)
                if strips:
                    anim_strips = strips
                    anim_duration_ms = max(1, cycle_ms)
        if png_card:
            try:
                from utils.quote_image import render_video_quote_gif
                gif_bytes = await render_video_quote_gif(
                    video_bytes, card_png_bytes=png_card,
                    anim_strips=anim_strips, anim_duration_ms=anim_duration_ms,
                    circle=is_video_note,
                )
            except Exception as e:
                logger.warning(
                    f"quote gif: render_video_quote_gif FAILED: {type(e).__name__}: {e}")
                gif_bytes = None
            if not png_card:
                logger.warning("quote gif: info strip render FAILED (pillow/fonts?)")
            elif not gif_bytes:
                logger.warning(
                    "quote gif: render_video_quote_gif returned None "
                    "(ffmpeg? palette? >12MB?)")
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
        avatar_bytes, date_str, background_bytes, inline_images,
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
