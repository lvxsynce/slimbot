"""Конвертация media (photo/video bytes) → GIF для команды `.вгф`.

Используется командой `.вгф`. Telethon ``send_file(buf, attributes=[DocumentAttributeAnimated()],
mime_type="image/gif")`` — Telegram рендерит как inline-playable GIF preview.

Два пути:
1. ``photo_to_gif_bytes(data)`` — JPEG/PNG/WEBP photo bytes → **статичная
   GIF**: 2 identical кадра по 5с, infinite loop, без zoom/wobble. Визуально
   ничего не движется, но ``DocumentAttributeAnimated()`` ставится на стороне
   Telegram → клиент рендерит именно как GIF preview (не как document со
   стрелочкой скачивания). Трюк с 2-я, а не 1-м кадром — против
   Telegram-эвристики «1 frame → static document».

2. ``video_to_gif_bytes(data)`` — video bytes → **анимированная GIF**:
   multi-frame с реальным движением. Требует **ffmpeg в PATH** (внешний
   бинарник, не Python-зависимость). Pipeline — одно-проходный с
   ``palettegen=stats_mode=full + paletteuse=dither=sierra2_4a`` после
   ``scale=<max_width>:-2:flags=lanczos`` (height auto, выравнивание по чётности
   для совместимости с libgif/x264-derived декодерами). Bounds (defaults):
    ``max_duration=8s``, ``max_fps=24``, ``max_width=960`` — сохраняем больше
    деталей и плавности движения. ``-an`` снимает
   аудио (в GIF его и некуда деть).

Безопасность:
- Photo-path: опциональный max_size (default 2048x2048) clamped через
  ``img.thumbnail`` для огромных 4K фоток.
- Video-path: bounds выше + ``asyncio.wait_for(timeout=60)`` против
  зависающих видео, ``proc.kill()`` по таймауту.
- ffmpeg stderr гасим через ``loglevel=error``; ненулевой exit / пустой stdout
  → return None.
"""

from __future__ import annotations

import asyncio
import io
import logging
import shutil

logger = logging.getLogger(__name__)


_PILLOW_AVAILABLE: bool | None = None


def is_available() -> bool:
    """True if Pillow is importable."""
    global _PILLOW_AVAILABLE
    if _PILLOW_AVAILABLE is None:
        try:
            from PIL import Image  # noqa: F401
            _PILLOW_AVAILABLE = True
        except Exception as e:
            logger.debug(f"gif_converter.is_available: Pillow unavailable: {e}")
            _PILLOW_AVAILABLE = False
    return _PILLOW_AVAILABLE


def _flatten_rgba(img):
    """RGBA → RGB на белом фоне (для совместимости с GIF — палитра)."""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        from PIL import Image
        bg = Image.new("RGB", img.size, (255, 255, 255))
        # Paste with alpha mask for proper blending.
        if img.mode in ("RGBA", "LA"):
            rgba = img.convert("RGBA")
            mask = rgba.split()[-1]
        else:
            # Convert palette+P mode with transparency to RGBA.
            rgba = img.convert("RGBA")
            mask = rgba.split()[-1]
        bg.paste(rgba, mask=mask)
        return bg
    if img.mode == "P":
        return img.convert("RGB")
    # CMYK, L, I, F — convert to RGB.
    return img.convert("RGB")


def photo_to_gif_bytes(
    data: bytes,
    *,
    max_size: tuple[int, int] | None = (2048, 2048),
) -> bytes | None:
    """Photo bytes → статичная GIF (2 identical frames по 5с, infinite loop).

    Args:
        data:     JPEG/PNG bytes (from ``reply.download_media(file=bytes)``).
        max_size: Optional ``(max_w, max_h)`` upper-bound clamp через
            ``img.thumbnail`` (default 2048x2048 — сохраняет больше деталей
                  разрешение → чётче preview при развороте, но и тяжелее GIF;
                  ``None`` отключает clamp).

    Returns:
        GIF bytes, или ``None`` if Pillow/PIL raises или bytes не декодируемы.

    Telegram: 2 IDENTICAL frames с длиной 5с + атрибут
    ``DocumentAttributeAnimated()`` → клиент рендерит именно как GIF preview,
    а не как document со стрелкой. Визуально статично (юзер смотрит 5с → ещё
    5с → wraparound идентичный кадр).

    Возможный bug: на старых клиентах Telegram (iOS/Android < 10) атрибут
    ``DocumentAttributeAnimated`` может игнорироваться → fallback к document
    со стрелкой. Резерв — браузер-просмотрщик в сообщении.
    """
    if not is_available():
        return None
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()  # force decode для валидации что bytes ok
    except Exception as e:
        logger.debug(f"photo_to_gif_bytes: Image.open failed: {e}")
        return None

    img = _flatten_rgba(img)

    # Safety: ограничиваем гигантские фото до max_size (default 2048x2048).
    # Photo в Telegram НЕ редко бывает 4K+ → без этого GIF во много МБ и
    # encode time tormozит event loop Telethon-клиента.
    if max_size is not None:
        max_w, max_h = max_size
        if img.width > max_w or img.height > max_h:
            img.thumbnail((max_w, max_h), Image.LANCZOS)

    # 2 IDENTICAL frame'а + 5с каждый: ``img`` (НЕ копия!) в Frame 0 и
    # ``img.copy()`` в Frame 1 — поэтому loop wrap даёт Frame 1 → Frame 0,
    # пиксельно идентичный переход, визуально «висим» на одной картинке, не
    # дёргает. ``disposal=2`` в .save() ниже дополнительно фиксирует «clear
    # to background before each frame» против любых остаточных артефактов.
    frames = [img, img.copy()]
    frame_durations = [5000, 5000]  # в мс: 5с/кадр × 2 кадра = 10с цикл loop'а

    # Quantize для GIF (требует ≤256 colors). Dither=Image.FLOYDSTEINBERG
    # критичен для читабельности текста: раскидывает ошибки
    # квантизации между соседними пикселями, предотвращает banding.
    quantized_frames = [
        f.convert("P", palette=Image.ADAPTIVE, colors=256, dither=Image.FLOYDSTEINBERG)
        for f in frames
    ]

    # Anti-collapse nudge для Pillow: её GIF plugin схлопывает 100% pixel-identical
    # кадры в один даже при optimize=False (key step внутри GifImagePlugin, не
    # отключается через kwarg'и). Если оставить кадры byte-identical, итоговый
    # GIF содержит 1 frame → срабатывает Telegram-эвристика «1 frame → static
    # document» и юзер видит обычный файл-скачивание вместо inline GIF preview.
    #
    # Флипаем младший бит palette-индекса пикселя (0,0) у Frame 1 ПОСЛЕ
    # квантизации — это делает кадр гарантированно byte-diff, при этом
    # визуально неотличимо (1 пиксель на 2048×2048 ниже порога восприятия;
    # ещё и в углу, где замечать нечего). XOR с 1 (а не +1) защищает от
    # палитр < 256 цветов где (p+1)%256 может выйти за диапазон.
    _p = quantized_frames[1].getpixel((0, 0))
    quantized_frames[1].putpixel((0, 0), _p ^ 1)

    buf = io.BytesIO()
    quantized_frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=quantized_frames[1:],
        duration=frame_durations,  # list per-frame durations
        loop=0,                    # infinite loop
        optimize=False,            # защитный belt-and-suspenders на случай если
                                   # в будущем Pillow добавит ещё одну оптимизацию
        disposal=2,                # restore to background between frames
    )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Video path: bytes → multi-frame animated GIF через ffmpeg (внешний бинарник).
# ---------------------------------------------------------------------------

_FFMPEG_AVAILABLE: bool | None = None


def is_ffmpeg_available() -> bool:
    """True если ``ffmpeg`` найден в PATH. Кешируется один раз."""
    global _FFMPEG_AVAILABLE
    if _FFMPEG_AVAILABLE is None:
        path = shutil.which("ffmpeg")
        if path is None:
            logger.debug("gif_converter: ffmpeg not in PATH")
            _FFMPEG_AVAILABLE = False
        else:
            logger.debug(f"gif_converter: ffmpeg found at {path}")
            _FFMPEG_AVAILABLE = True
    return _FFMPEG_AVAILABLE


async def video_to_gif_bytes(
    data: bytes,
    *,
    max_duration_s: float = 8.0,
    max_fps: int = 24,
    max_width: int = 960,
    timeout_s: float = 90.0,
) -> bytes | None:
    """Video bytes → анимированная GIF (multi-frame, реальное движение).

    Args:
        data:           bytes от ``reply.download_media(file=bytes)``. Любой
                        формат, который ffmpeg читает (MP4 / WEBM / MOV / AVI /
                        MKV / H.264 / VP9 / animated-photo MP4 / video_note).
                        Audio (если есть) снимается через ``-an`` (в GIF всё
                        равно звука нет).
        max_duration_s: Сколько секунд видео от начала берём (``-t``). Cap —
                        чтобы длинный ролик не превращался в гигантский GIF.
                        Default 8с.
        max_fps:        Целевой FPS для GIF. Default 24 — меньше рывков и
                        лучше передача быстрых сцен.
        max_width:      Целевая ширина в пикселях, height auto от aspect ratio
                        (``min(W,iw)`` не увеличивает маленькие видео; height
                        округляется до чётного для
                        совместимости). Default 960 — заметно чётче 720,
                        приближается к HD-восприятию при развороте на весь
                        экран, при этом размер файла остаётся в разумных
                        пределах.
        timeout_s:      Hard timeout на весь subprocess. Default 90с (выше
                        разрешение → дольше encode); при таймауте
                        ``proc.kill()`` и return None. Не блокирует event loop
                        (asyncio.create_subprocess_exec + asyncio.wait_for).

    Returns:
        GIF bytes, или ``None`` если ffmpeg недоступен / упал / таймаут /
        пустой stdout / ``data`` пустой.

    Pipeline (один вызов ffmpeg): ``fps=N, scale=min(W,iw):-2:flags=lanczos,
     split[s0][s1]; [s0]palettegen=stats_mode=full:reserve_transparent=0[p];
    [s1][p]paletteuse=dither=sierra2_4a`` — высокое качество через
    pre-computed full palette (полные 256 цветов по всему клипу, не по кадру),
    без промежуточного файла. ``-loop 0`` = infinite loop, как в photo-path.
    """
    if not is_ffmpeg_available():
        return None
    if not data:
        logger.debug("video_to_gif_bytes: empty input")
        return None

    # Filter chain делается одной строкой (ffmpeg принимает). Запятые внутри
    # `split[s0][s1]` — это branches, ffmpeg их разбирает.
    # palettegen=stats_mode=full — одна общая палитра на весь клип (не по
    # кадру), reserve_transparent=0 оставляет все 256 цветов изображению,
    # paletteuse=dither=sierra2_4a — качественный dither для переходов.
    vf = (
        f"fps={max_fps},"
        f"scale='min({max_width},iw)':-2:flags=lanczos,"
        f"split[s0][s1];"
        f"[s0]palettegen=stats_mode=full:reserve_transparent=0[p];"
        f"[s1][p]paletteuse=dither=sierra2_4a"
    )
    cmd = [
        "ffmpeg",
        "-loglevel", "error",   # гасим info/spam, errors оставим в stderr
        "-hide_banner",          # убираем баннер "ffmpeg version ..."
        "-y",                    # overwrite pipe (no-op для stdin/stdout, но вменяемо)
        "-ss", "0",              # start с начала
        "-t", f"{max_duration_s}",  # cap по длительности
        "-i", "pipe:0",          # read from stdin
        "-vf", vf,
        "-loop", "0",            # infinite loop (как в photo-path)
        "-an",                   # strip audio (в GIF нет и нечего ему тут)
        "-f", "gif",
        "pipe:1",                # write to stdout
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=data),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        logger.warning(
            f"video_to_gif_bytes: ffmpeg timeout after {timeout_s}s "
            f"(input={len(data)} bytes, max_width={max_width})"
        )
        return None
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip()[-200:]
        logger.debug(
            f"video_to_gif_bytes: ffmpeg failed (rc={proc.returncode}): {msg}"
        )
        return None
    if not stdout:
        logger.debug("video_to_gif_bytes: ffmpeg returned empty stdout")
        return None
    return stdout
