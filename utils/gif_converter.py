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
import os as _os
import shutil
import tempfile as _tempfile

logger = logging.getLogger(__name__)


def frames_to_gif_bytes(frames, durations=None, *, loop: int = 0,
                        max_size: tuple[int, int] | None = (800, 800),
                        colors: int = 256) -> bytes | None:
    """Список RGBA-кадров → GIF bytes. None если кадров < 2. Pure (Pillow).

    Нужна для анимированных стикеров (TGS/webm): rlottie и ffmpeg отдают
    кадры, а GIF-цитата хочет готовое видео. Общая палитра строится по
    монтажу тамбнейлов (MEDIANCUT), иначе покадровые палитры мерцают.
    """
    if not frames or len(frames) < 2:
        return None
    try:
        from PIL import Image
        prepared = []
        for fr in frames:
            im = fr.convert("RGBA")
            # Флэттен на чёрный: у GIF нет альфы, иначе кадр «мигает».
            bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
            bg.alpha_composite(im)
            im = bg.convert("RGB")
            if max_size:
                im.thumbnail(max_size, Image.LANCZOS)
            prepared.append(im)
        thumbs = []
        for im in prepared:
            t = im.copy()
            t.thumbnail((160, 160))
            thumbs.append(t)
        montage = Image.new(
            "RGB",
            (max(1, sum(t.width for t in thumbs)), max(t.height for t in thumbs)),
            (0, 0, 0),
        )
        x = 0
        for t in thumbs:
            montage.paste(t, (x, 0))
            x += t.width
        pal = montage.quantize(colors=colors, method=Image.MEDIANCUT)
        out_frames = [
            im.quantize(palette=pal, dither=Image.FLOYDSTEINBERG) for im in prepared
        ]
        n = len(out_frames)
        if durations and len(durations) == n:
            durs = [max(20, int(d or 100)) for d in durations]
        else:
            durs = [100] * n
        import io as _io
        buf = _io.BytesIO()
        out_frames[0].save(
            buf, format="GIF", save_all=True, append_images=out_frames[1:],
            duration=durs, loop=loop, disposal=2,
        )
        return buf.getvalue()
    except Exception as e:
        logger.debug(f"frames_to_gif_bytes failed: {e}")
        return None


def rgba_frame_to_png_bytes(frame) -> bytes | None:
    """RGBA-кадр → PNG bytes (для статичной карточки). None при ошибке."""
    if frame is None:
        return None
    try:
        import io as _io
        buf = _io.BytesIO()
        frame.convert("RGBA").save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.debug(f"rgba_frame_to_png_bytes failed: {e}")
        return None


# Режим «подогнать под число кадров»: fps = fit_frames / длительность.
MIN_FIT_FPS = 3.0            # ниже 3fps GIF выглядит как слайд-шоу
GIF_SOURCE_MAX_DURATION_S = 90.0   # абсолютный потолок окна для цитаты


def probe_duration(path: str) -> float | None:
    """Длительность медиафайла в секундах через ffprobe. None при ошибке.

    Нужна, чтобы fps подбирался под реальную длину клипа: у кружков она
    варьируется от 1 до 60 секунд, и фиксированные 4 секунды показывали
    только начало длинных.

    ВНИМАНИЕ: это БЛОКИРУЮЩИЙ ``subprocess.run``. Вызывать только через
    :func:`probe_duration_async`, который уводит его в поток — иначе event
    loop встаёт на всё время ffprobe (до 20 с) и в это время не может
    обслуживать ни один Telethon-клиент.
    """
    import subprocess
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, timeout=20)
        val = (p.stdout or b"").decode("utf-8", "replace").strip()
        return float(val) if val and float(val) > 0 else None
    except Exception as e:
        logger.debug(f"probe_duration failed: {e}")
        return None


async def probe_duration_async(path: str) -> float | None:
    """Неблокирующая обёртка над :func:`probe_duration`."""
    import asyncio
    return await asyncio.to_thread(probe_duration, path)


def _video_ext(data: bytes) -> str:
    """Расширение для temp-файла по magic bytes (для probing ffmpeg). Pure.

    Нужно, потому что вход теперь идёт файлом: ffmpeg детектит формат по
    содержимому, но расширение ускоряет/уточняет пробу (и даёт внятные ошибки
    в логе). Telegram-контейнеры: mp4/mov (ftyp), webm/mkv (EBML 0x1A45DFA3).
    """
    if data[:4] == b"\x1a\x45\xdf\xa3":
        # EBML: webm или mkv. Различить без парсинга нельзя, а webm-mkv
        # demuxer'ы в ffmpeg общий — .mkv подходит обоим.
        return ".webm"
    if data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3" or data[:2] == b"\x1f\x8b":
        return ".gz"
    if data[:4] == b"RIFF":
        return ".avi"
    if data[:3] == b"FLV":
        return ".flv"
    # Неизвестно — mp4 самый частый контейнер у Telegram-видео; ffmpeg всё
    # равно проверит содержимое и выдаст внятную ошибку, если это не он.
    return ".mp4"


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
    fit_frames: int = 0,
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
    def _build_vf(fps: float) -> str:
        return (
            f"fps={fps},"
            f"scale='min({max_width},iw)':-2:flags=lanczos,"
            f"split[s0][s1];"
            f"[s0]palettegen=stats_mode=full:reserve_transparent=0[p];"
            f"[s1][p]paletteuse=dither=sierra2_4a"
        )
    # ВХОД идёт через temp-файл, а НЕ через pipe:0.
    #
    # mp4 от Telegram — обычный (не faststart): атом moov лежит в КОНЦЕ файла.
    # На не-seekable stdin ffmpeg не может дочитать moov и рвёт разбор:
    #   [mov,mp4,...] stream 0, offset 0x30: partial file
    #   Error during demuxing: Invalid data found when processing input
    # (rc=183/234, пустой stdout). Мелкие файлы ffmpeg успевает целиком
    # забуферизовать — поэтому баг проявляется только на «взрослых» видео
    # (кружки ~1.5MB) и выглядит как «кружки не работают». Temp-файл
    # seekable, поэтому moov в конце читается нормально.
    tmp_path = None
    try:
        fd, tmp_path = _tempfile.mkstemp(suffix=_video_ext(data), prefix="v2gif_")
        with _os.fdopen(fd, "wb") as f:
            f.write(data)

        # Длительность клипа решает fps: режим fit_frames подгоняет частоту
        # так, чтобы на выходе было ~fit_frames кадров на ВСЮ длину ролика.
        # Раньше стоял фиксированный `-t max_duration_s` (4 с в цитате), и
        # кружок на 60 секунд показывал только первые секунды — «берётся
        # только начало». Теперь fps = fit_frames / duration (с полом/потолком),
        # а лишние кадры равномерно прореживает _sample_plan.
        #
        # ВАЖНО: vf собирается ЗДЕСЬ, а не выше. Раньше строка фильтра
        # строилась до подгонки fps, из-за чего fit_frames не давал НИКАКОГО
        # эффекта (мёртвый код), и длинный ролик кодировался на полном fps.
        window = float(max_duration_s)
        fps = float(max_fps)
        if fit_frames:
            probed = await probe_duration_async(tmp_path)
            if probed and probed > 0:
                window = min(probed, GIF_SOURCE_MAX_DURATION_S)
                want = fit_frames / max(window, 0.1)
                fps = max(MIN_FIT_FPS, min(fps, want))
            logger.debug(
                "video_to_gif_bytes: fit_frames=%s dur=%s window=%.2f fps=%.2f",
                fit_frames, probed, window, fps)
        vf = _build_vf(fps)
        cmd = [
            "ffmpeg",
            "-loglevel", "error",   # гасим info/spam, errors оставим в stderr
            "-hide_banner",          # убираем баннер "ffmpeg version ..."
            "-y",                    # overwrite output
            "-ss", "0",              # start с начала
            "-t", f"{window:.3f}",   # окно = длительность клипа (fit_frames)
            "-i", tmp_path,          # seekable input (см. комментарий выше)
            "-vf", vf,
            "-loop", "0",            # infinite loop (как в photo-path)
            "-an",                   # strip audio (в GIF нет и нечего ему тут)
            "-f", "gif",
            "pipe:1",                # write to stdout
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # CancelledError сюда приходит от внешнего
            # `wait_for(..., 300)` в telethon_manager.outgoing_handler.
            # Раньше ловился только TimeoutError, поэтому при отмене задачи
            # `proc.kill()` НЕ вызывался и ffmpeg оставался осиротевшим
            # процессом, жующим CPU и память.
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            logger.warning(
                f"video_to_gif_bytes: ffmpeg timeout/cancel after {timeout_s}s "
                f"(input={len(data)} bytes, max_width={max_width})"
            )
            return None
        if proc.returncode != 0:
            msg = stderr.decode("utf-8", errors="replace").strip()[-300:]
            logger.warning(
                f"video_to_gif_bytes: ffmpeg failed (rc={proc.returncode}, "
                f"input={len(data)}B): {msg}"
            )
            return None
        if not stdout:
            logger.warning(
                f"video_to_gif_bytes: ffmpeg returned empty stdout "
                f"(input={len(data)}B)"
            )
            return None
        return stdout
    finally:
        if tmp_path:
            try:
                _os.remove(tmp_path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# WEBM (видео-стикеры Telegram) → RGBA-кадры.
# ---------------------------------------------------------------------------

# Максимум кадров/размер для эмодзи — они рисуются мелко в строке текста.
EMOJI_MAX_FRAMES = 48
EMOJI_SIZE = 128
EMOJI_FPS = 24

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _split_png_stream(data: bytes) -> list[bytes]:
    """Разрезает concat-поток PNG (image2pipe) на отдельные файлы. Pure.

    ffmpeg пишет кадры подряд без разделителей, поэтому режем по сигнатуре
    ``\\x89PNG\\r\\n\\x1a\\n`` и отрезаем IEND-хвост каждого кадра.
    """
    if not data:
        return []
    out: list[bytes] = []
    pos = data.find(_PNG_MAGIC)
    while pos != -1:
        nxt = data.find(_PNG_MAGIC, pos + len(_PNG_MAGIC))
        end = nxt if nxt != -1 else len(data)
        iend = data.rfind(b"IEND", pos, end)
        if iend != -1:
            end = iend + 8  # len("IEND") + CRC32
        chunk = data[pos:end]
        if len(chunk) > len(_PNG_MAGIC):
            out.append(chunk)
        pos = nxt
    return out


async def webm_to_rgba_frames(
    data: bytes,
    *,
    fps: int = EMOJI_FPS,
    size: int = EMOJI_SIZE,
    max_frames: int = EMOJI_MAX_FRAMES,
    timeout_s: float = 30.0,
) -> list:
    """WEBM-стикер → список PIL RGBA-кадров. ``[]`` если нечем/упало.

    Формат Telegram: VP9 + альфа-канал, лежащий в Matroska
    ``BlockAdditions(0x75A1) → BlockMore(0xA6) → BlockAdditional(0xA5)``.
    ffmpeg-демуксер этот сайд-канал НЕ читает — без ``-c:v libvpx-vp9``
    перед ``-i`` кадры приходят без альфы (см. ``alpha_lost`` ниже), и мы
    получаем непрозрачные квадраты вместо вырезки эмодзи. Флаг на входе
    заставляет libvpx-декодер поднять ``yuva420p`` вместо ``yuv420p``.

    Кадры отдаём через ``image2pipe`` + PNG (а не GIF): у GIF прозрачность
    ровно 1 бит и палитра 256 цветов — альфа и плавность теряются.

    ``alpha_lost`` в логе означает «файл без альфы» (обычные видео-стикеры
    без прозрачности) — это не ошибка, кадры всё равно пригодятся.
    """
    if not data or not is_ffmpeg_available():
        return []
    cmd = [
        "ffmpeg", "-loglevel", "error", "-hide_banner", "-y",
        "-c:v", "libvpx-vp9",     # ОБЯЗАТЕЛЬНО до -i: поднимает yuva420p
        "-i", "pipe:0",
        "-vf", f"fps={fps},scale={size}:-1:flags=lanczos,format=rgba",
        "-vsync", "0",            # не дублировать кадры по vsync
        "-frames:v", str(max_frames),
        "-f", "image2pipe", "-vcodec", "png",
        "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        logger.debug(f"webm_to_rgba_frames: spawn failed: {e}")
        return []
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=data), timeout=timeout_s,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # CancelledError — от внешнего wait_for в outgoing_handler. Без него
        # в kill() ffmpeg-процесс утекал бы сиротой.
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        logger.debug("webm_to_rgba_frames: ffmpeg timeout/cancel")
        return []
    if proc.returncode != 0 or not stdout:
        logger.debug(
            f"webm_to_rgba_frames: ffmpeg failed rc={proc.returncode}: "
            f"{stderr.decode('utf-8', errors='replace').strip()[-160:]}"
        )
        return []

    def _parse() -> list:
        try:
            from PIL import Image
            import io as _io
            frames = []
            for png in _split_png_stream(stdout):
                try:
                    frames.append(Image.open(_io.BytesIO(png)).convert("RGBA"))
                except Exception:
                    continue
                if len(frames) >= max_frames:
                    break
            return frames
        except Exception as e:
            logger.debug(f"webm_to_rgba_frames: parse failed: {e}")
            return []

    try:
        loop = asyncio.get_running_loop()
        frames = await loop.run_in_executor(None, _parse)
    except Exception:
        return []
    if frames:
        lo = min(f.getchannel("A").getextrema()[0] for f in frames)
        if lo == 255:
            logger.debug("webm_to_rgba_frames: alpha_lost (нет BlockAdditions)")
    return frames


def webm_frame_durations(n_frames: int, fps: int = EMOJI_FPS) -> list[int]:
    """Равномерные длительности кадров (мс) для n_frames. Pure."""
    if n_frames <= 0:
        return []
    return [max(20, int(1000 / max(1, fps)))] * n_frames


def tgs_to_rgba_frames(
    data: bytes,
    *,
    max_frames: int = EMOJI_MAX_FRAMES,
    size: int = EMOJI_SIZE,
) -> list:
    """TGS (gzip+Lottie) → список PIL RGBA-кадров. ``[]`` если недоступно.

    Единственный реальный растеризатор Lottie в Python — ``rlottie-python``
    (abi3-колесо, тянет за собой rlottie). Импорт ленивый: без пакета функция
    молча возвращает ``[]``, и caller рисует статичный глиф.

    Кадры выбираются равномерно по всему циклу анимации, потому что
    Telegram-TGS обычно 60fps, а в цитате нужно ~24.
    """
    if not data:
        return []
    try:
        from rlottie_python import LottieAnimation
    except Exception as e:
        logger.debug(f"tgs_to_rgba_frames: rlottie-python unavailable: {e}")
        return []
    if len(data) < 2 or data[0] != 0x1F or data[1] != 0x8B:
        # Не gzip — возможно это уже голый .json Lottie.
        return _lottie_json_to_frames(data, max_frames=max_frames, size=size)
    import os as _os
    import tempfile as _tempfile
    from PIL import Image

    tmp = None
    try:
        fd, tmp = _tempfile.mkstemp(suffix=".tgs", prefix="emoji_")
        with _os.fdopen(fd, "wb") as f:
            f.write(data)
        anim = LottieAnimation.from_tgs(tmp)
        return _render_lottie(anim, max_frames=max_frames, size=size)
    except Exception as e:
        logger.debug(f"tgs_to_rgba_frames: render failed: {e}")
        return []
    finally:
        if tmp:
            try:
                _os.remove(tmp)
            except Exception:
                pass


def _lottie_json_to_frames(
    data: bytes, *, max_frames: int, size: int
) -> list:
    """Голый Lottie-JSON (без gzip) → RGBA-кадры. Pure-обёртка. ``[]`` при ошибке."""
    try:
        from rlottie_python import LottieAnimation
    except Exception:
        return []
    try:
        return _render_lottie(
            LottieAnimation.from_data(data.decode("utf-8", "replace")),
            max_frames=max_frames, size=size,
        )
    except Exception as e:
        logger.debug(f"_lottie_json_to_frames: failed: {e}")
        return []


def _render_lottie(anim, *, max_frames: int, size: int) -> list:
    """Рендер rlottie-анимации в равномерные RGBA-кадры размера size. Pure-CPU."""
    from PIL import Image
    try:
        total = int(anim.lottie_animation_get_totalframe() or 0)
    except Exception:
        return []
    if total <= 0:
        return []
    n = max(1, min(max_frames, total))
    frames: list = []
    for i in range(n):
        # Равномерно по циклу; при n==1 берём последний кадр (обычно самый «полный»).
        idx = int(round(i * (total - 1) / (n - 1))) if n > 1 else total - 1
        try:
            im = anim.render_pillow_frame(frame_num=idx)
        except Exception:
            continue
        if im is None:
            continue
        try:
            im = im.convert("RGBA")
            if im.width != size or im.height != size:
                # Квадратная посадка: эмодзи всегда 1:1, иначе прыгает базовая линия.
                im = im.resize((size, size), Image.LANCZOS)
        except Exception:
            continue
        frames.append(im)
    return frames


def apply_circle_mask(img, margin_ratio: float = 0.04):
    """Круглая альфа-маска по центру (для кружков video_note). Возвращает RGBA.

    Telegram отдаёт кружок квадратным mp4 200x200, а круглую маску клиент
    рисует сам. Чтобы цитата выглядела как кружок, маску применяем здесь.
    Ничего не меняет для обычных видео.

    ``margin_ratio`` — отступ от краёв кадра. Без него круг упирается в
    границы холста и визуально читается как «обрезанный по краям».
    """
    if img is None:
        return None
    from PIL import Image, ImageDraw
    try:
        im = img.convert("RGBA")
        w, h = im.size
        m = min(w, h) * max(0.0, min(0.2, float(margin_ratio)))
        box = (m, m, w - 1 - m, h - 1 - m)
        if box[2] <= box[0] or box[3] <= box[1]:
            box = (0, 0, w - 1, h - 1)
        # Сглаживание края: маску рисуем в 4x supersample, иначе круг ступенчатый.
        ss = 4
        big = Image.new("L", (w * ss, h * ss), 0)
        ImageDraw.Draw(big).ellipse(
            (box[0] * ss, box[1] * ss, box[2] * ss, box[3] * ss), fill=255
        )
        smooth = big.resize(im.size, Image.LANCZOS)
        im.putalpha(smooth)
        return im
    except Exception as e:
        logger.debug(f"apply_circle_mask failed: {e}")
        return img


async def image_audio_to_video_bytes(
    card_png: bytes,
    audio_bytes: bytes,
    *,
    max_width: int = 960,
    fps: int = 2,
    timeout_s: float = 120.0,
) -> bytes | None:
    """Статичная карточка + звук → MP4 (видео-цитата для голосовых/аудио).

    Кадр зациклен на всю длину аудио (``-loop 1 -shortest``), видео —
    libx264 ultrafast, звук — AAC. Возвращает MP4 bytes или None
    (нет ffmpeg / encode упал / таймаут).
    """
    if not card_png or not audio_bytes:
        return None
    if not is_ffmpeg_available():
        return None
    import asyncio as _asyncio
    import os as _os
    import tempfile as _tempfile
    # Картинку кладём во временный файл (два stdin-пайпа через
    # communicate не скормить), аудио идёт в stdin.
    tmp = None
    try:
        fd, tmp = _tempfile.mkstemp(suffix=".png", prefix="qcard_")
        with _os.fdopen(fd, "wb") as f:
            f.write(card_png)
        cmd2 = [
            "ffmpeg",
            "-loglevel", "error",
            "-hide_banner",
            "-y",
            "-loop", "1",
            "-framerate", str(fps),
            "-i", tmp,
            "-i", "pipe:0",
            "-map", "0:v",
            "-map", "1:a",
            "-vf", f"scale='min({max_width},iw)':-2:flags=lanczos,format=yuv420p",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "96k",
            "-shortest",
            "-movflags", "frag_keyframe+empty_moov",
            "-f", "mp4",
            "pipe:1",
        ]
        proc2 = await _asyncio.create_subprocess_exec(
            *cmd2,
            stdin=_asyncio.subprocess.PIPE,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await _asyncio.wait_for(
                proc2.communicate(input=audio_bytes),
                timeout=timeout_s,
            )
        except (_asyncio.TimeoutError, _asyncio.CancelledError):
            # См. комментарий в video_to_gif_bytes: отмена задачи тоже
            # обязана убивать процесс, иначе он живёт сиротой.
            proc2.kill()
            try:
                await proc2.wait()
            except Exception:
                pass
            logger.warning("image_audio_to_video_bytes: ffmpeg timeout/cancel")
            return None
        if proc2.returncode != 0:
            msg = stderr.decode("utf-8", errors="replace").strip()[-200:]
            logger.debug(f"image_audio_to_video_bytes: ffmpeg failed: {msg}")
            return None
        if not stdout:
            return None
        return stdout
    finally:
        if tmp:
            try:
                _os.remove(tmp)
            except Exception:
                pass
