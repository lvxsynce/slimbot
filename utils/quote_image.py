"""PNG-рендер цитат v2 — прямоугольный layout с аватаркой и bubble-overlay.

Used by `handlers/commands/quote.py` в Telethon-слое: вместо HTML-текста шлёт
картинку-цитату в виде:
    ┌─────────────────────────────────┐
    │                                  │
    │  [avatar]  Name                   │
    │            ID: 123 · @user · t    │  ← полу-прозрачный bubble
    │                                  │
    │  Body text...)                   │
    │  More body text...               │
    │                                  │
    └─────────────────────────────────┘

Layout components:
- Avatar — круглый кроп (80x80) или пропуск если нет фото
- Header: имя (accent color) + строка инфо (ID, @usernames, timestamp) dim
- Body — обёрнут в полупрозрачный серый rounded rectangle (bubble)
- Полностью прямоугольный (wide-and-short, AUTO_HEIGHT)

Fonts:
- Noto Sans core для Latin/Cyrillic/Greek/Arabic/Hebrew/Thai/Devanagari
- Noto Sans CJK для Chinese/Japanese/Korean (auto-detect по тексту)
- DejaVu Sans как final fallback

Если Pillow/font недоступны — handler падает на graceful HTML-text fallback,
поэтому Pillow не строгий dependency.
"""

from __future__ import annotations

import html as _html
import io
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ===== Layout constants =====
WIDTH = 1200
PADDING_X = 50
PADDING_Y = 40
AVATAR_SIZE = 80

# Sizes шрифтов
NAME_FONT_SIZE = 26
INFO_FONT_SIZE = 22
BODY_FONT_SIZE = 24

# Line heights
NAME_LINE_HEIGHT = 36
INFO_LINE_HEIGHT = 30
BODY_LINE_HEIGHT = 38

# Gap между header-блоком и bubble
HEADER_TO_BUBBLE_GAP = 18

# Bubble inset (внутри PADDING_X)
BUBBLE_INSET_X = 24
# Padding text внутри bubble (сверху/снизу + left+right)
BUBBLE_TEXT_PAD_X = 20
BUBBLE_TEXT_PAD_Y = 22

# Body wrap limits
MAX_BODY_LINES = 30  # truncate after this many body lines (защита от runaway)

# Canvas min/max
MIN_HEIGHT = 240
MAX_HEIGHT = 2400

# ===== Colors =====
# Solid RGB background (canvas)
BG_COLOR = (32, 33, 38)
# Accent for sender name
NAME_COLOR = (143, 168, 220)        # soft blue (как было)
# Dim для info-строки (ID · @user · timestamp)
INFO_COLOR = (170, 175, 185)
# Bright for body text
TEXT_COLOR = (240, 240, 240)
# RGBA полупрозрачный серый для bubble overlay (alpha = 180 / 255 = 70% opacity)
BUBBLE_COLOR = (45, 46, 50, 180)

# ===== Font fallback chain =====
# Primary: Noto Sans (modern Unicode coverage для Latin/Cyrillic/Greek/Arabic/Hebrew/Thai).
# Secondary: Noto Sans CJK (Chinese/Japanese/Korean — отдельный .ttc файл с CJK ranges).
# Final fallback: DejaVu Sans (только Latin/Cyrillic/Greek).
_FONT_PATHS = (
    Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)
_FALLBACK_FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


# ===== Color emoji strip =====
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001F02F"
    "\U0001F0A0-\U0001F0FF"
    "\U0001F100-\U0001F1FF"
    "\U0001F200-\U0001F2FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F650-\U0001F67F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U0001FB00-\U0001FBFF"
    "\U0001FC00-\U0001FCFF"
    "\U0001FD00-\U0001FDFF"
    "\U0001FE00-\U0001FEFF"
    "\U0001FF00-\U0001FFEF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "\u2300-\u23FF"
    "]+",
    flags=re.UNICODE,
)


# ===== Pillow + font availability =====

def _try_pillow() -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        return True
    except Exception as e:
        logger.debug(f"_try_pillow failed: {e}")
        return False


_PILLOW_AVAILABLE: bool | None = None


def is_available() -> bool:
    global _PILLOW_AVAILABLE
    if _PILLOW_AVAILABLE is None:
        _PILLOW_AVAILABLE = _try_pillow() and _resolve_font(NAME_FONT_SIZE) is not None
    return _PILLOW_AVAILABLE


def _contains_cjk(text: str) -> bool:
    """Detect CJK codepoints (Chinese/Japanese/Korean) in text.

    Используется для smart font selection в _resolve_font — если текст содержит
    CJK, нужно грузить NotoSansCJK font, иначе Noto Sans core.
    """
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF      # CJK Unified Ideographs
            or 0x3040 <= cp <= 0x309F    # Hiragana
            or 0x30A0 <= cp <= 0x30FF    # Katakana
            or 0xAC00 <= cp <= 0xD7AF    # Hangul Syllables
            or 0x3400 <= cp <= 0x4DBF    # CJK Extension A
        ):
            return True
    return False


def _resolve_font(size: int, text: str = ""):
    """Resolve truetype font с smart CJK detection.

    Параметр ``text`` опционален — если передан и содержит CJK codepoints,
    первым пробуется NotoSansCJK.ttc, иначе Noto Sans core. Финальный
    fallback — chain через ``_FONT_PATHS`` → ``_FALLBACK_FONT_PATH`` →
    ``ImageFont.load_default()``. None если вообще ничего не работает.
    """
    from PIL import ImageFont

    has_cjk = bool(text) and _contains_cjk(text)

    candidates: list[Path] = []
    if has_cjk:
        candidates.append(Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"))
        candidates.append(Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"))
    candidates.extend([
        Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    ])

    for p in candidates:
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size=size)
            except Exception as e:
                logger.debug(f"_resolve_font({p}, {size}) failed: {e}")
                continue

    for p in _FONT_PATHS:
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size=size)
            except Exception:
                continue
    if _FALLBACK_FONT_PATH.exists():
        try:
            return ImageFont.truetype(str(_FALLBACK_FONT_PATH), size=size)
        except Exception:
            pass
    try:
        return ImageFont.load_default()
    except Exception as e:
        logger.debug(f"_resolve_font load_default failed: {e}")
        return None


def _strip_html(text: str) -> str:
    """Strip HTML + unescape + remove color emoji + collapse repeated spaces.

    Коллапсим ТОЛЬКО repeated spaces (через ``re.sub(r" {2,}", " ", ...)``),
    НЕ \n — параграфы (\\n\\n) важны для multi-line сообщений в Telegram.
    """
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r" {2,}", " ", text)
    text = text.replace("\u00a0", " ").strip()
    return text


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    """Word-wrap text → list of strings длиной до max_width pixels.

    Слова длиннее max_width бьются посимвольно — иначе строка
    уезжает за canvas.
    """
    if not text:
        return []

    def _measure(s: str) -> float:
        try:
            return font.getlength(s)
        except Exception:
            return len(s) * 8

    def _break_long(word: str) -> list[str]:
        parts, cur = [], ""
        for ch in word:
            if cur and _measure(cur + ch) > max_width:
                parts.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            parts.append(cur)
        return parts or [word]

    lines: list[str] = []
    for raw_line in text.split("\n"):
        words = raw_line.split()
        if not words:
            lines.append("")
            continue
        chunks: list[str] = []
        for word in words:
            if _measure(word) > max_width:
                chunks.extend(_break_long(word))
            else:
                chunks.append(word)
        current = chunks[0]
        for word in chunks[1:]:
            candidate = current + " " + word
            if _measure(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _circle_avatar(avatar_bytes: bytes):
    """Return PIL RGBA Image of circular-cropped avatar, or None on failure."""
    from PIL import Image, ImageDraw
    try:
        av = Image.open(io.BytesIO(avatar_bytes))
        av = av.convert("RGBA").resize(
            (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS
        )
        mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
        ImageDraw.Draw(mask).ellipse(
            (0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255
        )
        av.putalpha(mask)
        return av
    except Exception as e:
        logger.debug(f"_circle_avatar failed: {e}")
        return None


# ===== Main entry point v2 =====

def render_quote_png(
    *,
    body: str,
    sender_name: str = "",
    sender_id: int | None = None,
    usernames: list[str] | None = None,
    avatar_bytes: bytes | None = None,
    timestamp: str = "",
) -> bytes | None:
    """Render прямоугольной PNG-цитаты с avatar + bubble overlay.

    Args:
        body: Текст цитаты (plain text — без HTML).
        sender_name: Имя отправителя (first + last).
        sender_id: User ID (int).
        usernames: Список @username (cap 2 в layout). None или [] → нет.
        avatar_bytes: PNG/JPEG bytes аватарки. None → без аварки.
        timestamp: Строка даты (например, "2026-07-18 18:30") для info-блока.

    Returns:
        PNG bytes (RGB, не RGBA — для universal preview в Telegram).
        ``None`` если Pillow/fonts недоступны.

    Layout (прямоугольный):
        ┌─────────────────────────────────┐
        │ [avatar]  Sender Name           │
        │            ID: 123 · @u · t    │
        │   ┌─────────────────────────┐   │
        │   │  body text...           │   │  ← полупрозрачный bubble
        │   │  more body text...      │   │
        │   └─────────────────────────┘   │
        └─────────────────────────────────┘
    """
    if not is_available():
        return None

    from PIL import Image, ImageDraw

    body_text = _strip_html(body)
    if not body_text:
        return None

    sender_name_text = _strip_html(sender_name) or "(unknown)"
    unames = (usernames or [])[:2]
    timestamp_text = _strip_html(timestamp)

    # --- Инфо-строка (строим ДО font resolve, чтобы font был aware о содержимом) ---
    info_parts: list[str] = []
    if sender_id:
        info_parts.append(f"ID: {sender_id}")
    for u in unames:
        info_parts.append(f"@{u}")
    if timestamp_text:
        info_parts.append(timestamp_text)
    info_str = " · ".join(info_parts)

    name_font = _resolve_font(NAME_FONT_SIZE, sender_name_text)
    info_font = _resolve_font(INFO_FONT_SIZE, info_str)
    body_font = _resolve_font(BODY_FONT_SIZE, body_text)

    if not (name_font and info_font and body_font):
        return None

    # --- Аватарка (опционально) ---
    avatar_img = _circle_avatar(avatar_bytes) if avatar_bytes else None

    # --- Wrap body ---
    bubble_text_max_width = WIDTH - 2 * PADDING_X - 2 * BUBBLE_INSET_X - 2 * BUBBLE_TEXT_PAD_X
    body_lines = _wrap_text(body_text, body_font, max_width=bubble_text_max_width)
    if len(body_lines) > MAX_BODY_LINES:
        body_lines = body_lines[:MAX_BODY_LINES]
        if body_lines:
            body_lines[-1] = body_lines[-1].rstrip() + "…"

    # --- Высота canvas ---
    header_text_height = NAME_LINE_HEIGHT + INFO_LINE_HEIGHT + 6
    header_height = max(AVATAR_SIZE, header_text_height)
    body_block_h = len(body_lines) * BODY_LINE_HEIGHT
    bubble_height = body_block_h + 2 * BUBBLE_TEXT_PAD_Y

    total_height = (
        PADDING_Y + header_height
        + HEADER_TO_BUBBLE_GAP
        + bubble_height
        + PADDING_Y
    )
    total_height = min(MAX_HEIGHT, max(MIN_HEIGHT, total_height))

    # --- Canvas: RGB (universal preview) ---
    img = Image.new("RGB", (WIDTH, total_height), color=BG_COLOR)

    # --- Header: avatar + name + info ---
    avatar_x = PADDING_X
    avatar_y = PADDING_Y + (header_height - AVATAR_SIZE) // 2 if avatar_img else PADDING_Y
    text_x = PADDING_X
    if avatar_img is not None:
        try:
            img.paste(avatar_img, (avatar_x, avatar_y), mask=avatar_img.split()[3])
        except Exception:
            img.paste(avatar_img.convert("RGB"), (avatar_x, avatar_y))
        text_x = avatar_x + AVATAR_SIZE + 20

    name_y = PADDING_Y + (header_height - header_text_height) // 2
    draw = ImageDraw.Draw(img)
    draw.text(
        (text_x, name_y),
        sender_name_text,
        fill=NAME_COLOR,
        font=name_font,
    )
    if info_str:
        info_y = name_y + NAME_LINE_HEIGHT + 2
        draw.text(
            (text_x, info_y),
            info_str,
            fill=INFO_COLOR,
            font=info_font,
        )

    # --- Semi-transparent bubble overlay (RGBA) ---
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    bubble_y_top = PADDING_Y + header_height + HEADER_TO_BUBBLE_GAP
    bubble_box = (
        PADDING_X + BUBBLE_INSET_X,
        bubble_y_top,
        WIDTH - PADDING_X - BUBBLE_INSET_X,
        bubble_y_top + bubble_height,
    )
    overlay_draw.rounded_rectangle(
        bubble_box, radius=16, fill=BUBBLE_COLOR,
    )
    img.paste(overlay, (0, 0), mask=overlay)

    # --- Body текст поверх bubble ---
    draw = ImageDraw.Draw(img)
    text_y = bubble_y_top + BUBBLE_TEXT_PAD_Y
    text_x = PADDING_X + BUBBLE_INSET_X + BUBBLE_TEXT_PAD_X
    for line in body_lines:
        draw.text(
            (text_x, text_y),
            line,
            fill=TEXT_COLOR,
            font=body_font,
        )
        text_y += BODY_LINE_HEIGHT

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
