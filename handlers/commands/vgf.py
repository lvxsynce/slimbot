"""Команда .вгф / .vfh / .gif — reply на photo/video → конвертировать в GIF
→ отправить ответом (reply на ОРИГИНАЛЬНОЕ media).

Telethon-only потому что:
- Нужен MTProto download_media для получения bytes replied media.
- Нужен Telethon-side send_file с подходящим MIME для inline GIF preview.

Args: нет (всегда требуется reply на photo/video; никаких extra-токенов).

Out:
- reply на photo → исходное изображение без повторного сжатия, отправленное
  как статичная GIF с двумя кадрами для GIF-превью Telegram.
- reply на video / video_note / animated-photo → **анимированная** GIF
  (multi-frame с реальным движением) через ffmpeg pipeline:
   ``fps=24, scale=min(960,iw):-2:flags=lanczos, palettegen=stats_mode=full:
  reserve_transparent=0, paletteuse=dither=sierra2_4a``. Bounds: max 8с.
  Требует
  ``ffmpeg`` в PATH (внешний бинарник, не Python-зависимость).
   Audio снимается (``-an``) — в GIF звука всё равно нет.

Telegram рендерит оба варианта как inline-playable preview благодаря флагу
``DocumentAttributeAnimated()`` (см. utils/gif_converter.py).

Сценарии:
- reply отсутствует → "[?] ответь этой командой на фото или видео"
- reply на sticker / text / unsupported → "[?] это не фото и не видео"
- ffmpeg недоступен → "[?] ffmpeg не найден в PATH" (только video-path)
- normal → результат отправлен reply'ем на ОРИГИНАЛЬНОЕ media, исходная команда
  удалена.
"""

import asyncio
import io
import logging

from telethon.tl.types import DocumentAttributeAnimated
from handlers.commands._base import command_card

VGF_CMDS = (".вгф", ".vfg", ".gif")

logger = logging.getLogger(__name__)

# Лимит размера скачиваемого media против OOM: download_media(file=bytes)
# держит ВЕСЬ файл в памяти. Видео (через ffmpeg, сжатие) — до 25 МБ,
# фото (Pillow, максимальный исходник) — до 10 МБ.
MAX_VGIF_DOWNLOAD_PHOTO = 10 * 1024 * 1024
MAX_VGIF_DOWNLOAD_VIDEO = 25 * 1024 * 1024


def _media_size_bytes(reply) -> int | None:
    """Размер replied media в байтах (из метаданных, без скачивания)."""
    photo = getattr(reply, "photo", None)
    if photo is not None:
        sizes = getattr(photo, "sizes", None) or []
        vals = [getattr(s, "size", 0) or 0 for s in sizes]
        return max(vals) if vals else None
    document = getattr(reply, "document", None)
    if document is not None:
        return int(getattr(document, "size", 0) or 0)
    return None


def _check(t: str | None) -> bool:
    if not t:
        return False
    head = t.strip().lower().split()[0]
    return head in VGF_CMDS


async def handle(user_id: str, event) -> None:
    """Telethon-вызов из telethon_manager._handle_outgoing.

    Args не парсим. Ветка выбирается по типу replied media:
    - photo (MessageMediaPhoto) → ``photo_to_gif_bytes`` (статичная GIF).
    - video / video_note / animation (Telegram-«анимированное фото») →
      ``video_to_gif_bytes`` (анимированная GIF, требует ffmpeg).
    Остальные media-типы (стикеры, голосовые, документы) — отклоняем.
    """
    from utils.telethon_manager import telethon_reply_to

    reply = await event.get_reply_message()
    if not reply:
        await event.edit(
            command_card("GIF", "[?] Ответь этой командой на фото или видео."),
            parse_mode="html",
        )
        return

    # Классификация media: photo vs video-like. ``video_note`` — круглые
    # видео-кружочки, ``animation`` — Telegram-«анимированное фото» (хранится
    # как video/mp4 документ с DocumentAttributeAnimated). Все три идут в
    # video-path через ffmpeg. Стикеры и текстовые сообщения отклоняем.
    photo_obj = getattr(reply, "photo", None)
    is_video = bool(getattr(reply, "video", None))
    is_video_note = bool(getattr(reply, "video_note", None))
    is_animation = bool(getattr(reply, "animation", None))
    is_sticker = bool(getattr(reply, "sticker", None))

    is_video_like = is_video or is_video_note or is_animation
    is_media_ok = bool(photo_obj) or is_video_like

    if not is_media_ok or is_sticker:
        await event.edit(
            command_card("GIF", "[?] Это не фото и не видео. Стикеры и прочее пока не поддерживаются."),
            parse_mode="html",
        )
        return

    # Tool availability check — специфично для ветки.
    if is_video_like:
        from utils.gif_converter import is_ffmpeg_available
        if not is_ffmpeg_available():
            await event.edit(
                command_card("GIF", "[?] Не могу конвертировать видео: ffmpeg не найден в PATH."),
                parse_mode="html",
            )
            return

    # Лимит размера ДО скачивания: метаданные есть всегда, а download_media
    # (file=bytes) держит весь файл в памяти — 500МБ видео = 500МБ RAM.
    size = _media_size_bytes(reply)
    cap = MAX_VGIF_DOWNLOAD_VIDEO if is_video_like else MAX_VGIF_DOWNLOAD_PHOTO
    if size is not None and size > cap:
        await event.edit(
            command_card(
                "GIF",
                f"[x] Файл слишком большой: {size // (1024 * 1024)} МБ "
                f"(лимит {cap // (1024 * 1024)} МБ).",
            ),
            parse_mode="html",
        )
        return

    # Скачиваем media в bytes. Telethon сам подберёт transport по размеру
    # (mtproto для больших > 10 МБ, иначе прямой download).
    try:
        data = await reply.download_media(file=bytes)
    except Exception as e:
        logger.warning(f"vgf: download_media failed: {e}")
        await event.edit(
            command_card("GIF", f"[x] Скачивание не удалось: {type(e).__name__}"),
            parse_mode="html",
        )
        return
    if not data:
        await event.edit(
            command_card("GIF", "[x] Не удалось получить данные медиа."),
            parse_mode="html",
        )
        return

    # Фото конвертируем в GIF. Формат ограничен 256 цветами, но это сохраняет
    # требуемое GIF-превью Telegram.
    if is_video_like:
        from utils.gif_converter import video_to_gif_bytes
        try:
            gif_bytes = await video_to_gif_bytes(data)
        except Exception as e:
            logger.warning(f"vgf: video_to_gif_bytes failed: {e}")
            gif_bytes = None
    else:
        from utils.gif_converter import photo_to_gif_bytes
        try:
            gif_bytes = await asyncio.get_running_loop().run_in_executor(
                None, photo_to_gif_bytes, data
            )
        except Exception as e:
            logger.warning(f"vgf: photo_to_gif_bytes failed: {e}")
            gif_bytes = None

    if not gif_bytes:
        await event.edit(
            command_card("GIF", "[x] Конвертация в GIF не удалась (unsupported format или ошибка обработки)."),
            parse_mode="html",
        )
        return

    # И фото, и видео отправляем как GIF с animation flag.
    try:
        buf = io.BytesIO(gif_bytes)
        buf.name = "sticker.gif"
        await event.client.send_file(
            event.chat_id,
            file=buf,
            reply_to=telethon_reply_to(event),  # ответ на ОРИГИНАЛЬНОЕ media
            force_document=False,
            attributes=[DocumentAttributeAnimated()],
            mime_type="image/gif",
        )
    except Exception as e:
        logger.warning(f"vgf: send_file failed: {e}")
        try:
            await event.edit(
                command_card("GIF", f"[x] Отправка не удалась: {type(e).__name__}: {str(e)[:200]}"),
                parse_mode="html",
            )
        except Exception:
            pass
        return

    # Best-effort: удаляем исходную `.вгф` команду.
    try:
        await event.delete()
    except Exception as e:
        logger.debug(f"vgf: delete origin failed: {e}")
