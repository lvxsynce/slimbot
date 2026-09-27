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

import asyncio
import html as _html
import io
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ===== Layout constants (banner) =====
WIDTH = 1400
PADDING_X = 60
PADDING_Y = 48
AVATAR_SIZE = 110

# Sizes шрифтов
NAME_FONT_SIZE = 34
INFO_FONT_SIZE = 28
BODY_FONT_SIZE = 32

# Line heights
NAME_LINE_HEIGHT = 46
INFO_LINE_HEIGHT = 38
BODY_LINE_HEIGHT = 48

# Gap между header-блоком и bubble
HEADER_TO_BUBBLE_GAP = 22

# Bubble inset (внутри PADDING_X)
BUBBLE_INSET_X = 28
# Padding text внутри bubble (сверху/снизу + left+right)
BUBBLE_TEXT_PAD_X = 24
BUBBLE_TEXT_PAD_Y = 26

# Body wrap limits
MAX_BODY_LINES = 30  # truncate after this many body lines (защита от runaway)

# Canvas min/max
MIN_HEIGHT = 240
MAX_HEIGHT = 2400

# Вертикальный масштаб карточки: все вертикальные метрики (отступы,
# межстрочные интервалы, gaps) умножаются на это. Ширина и размер
# шрифтов не меняются.
VERTICAL_SCALE = 2.5

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
# Применяется ТОЛЬКО если нет emoji-шрифта (см. _emoji_base_font):
# без него Pillow нечем рисовать цветные глифы и был бы tofu.
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
_EMOJI_FONT_PATH = Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf")
# Noto Color Emoji — CBDT-bitmap шрифт, рабочий strike только 109px.
# Глифы рендерим в 109px и даунскейлим под целевой размер (кэш _emoji_glyph).
_EMOJI_STRIKE_SIZE = 109
# Zero-width / модификаторы: не рисуем отдельно (иначе tofu), advance 0.
_EMOJI_SKIP = {"\u200d", "\ufe0f"}
_EMOJI_RUN_RE = re.compile(
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
    "\u20E3"
    "\u200d"
    "\ufe0f"
    "]+",
    flags=re.UNICODE,
)
_emoji_base = None
_emoji_glyph_cache: dict = {}


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


# ===== Per-character font fallback (unicode coverage) =====
# Один шрифт не покрывает весь Unicode: математические alphanumeric
# (U+1D400–U+1D7FF, напр. 𝒍𝒆𝒗𝒆𝒍𝒔), символы, CJK — у каждого свой файл.
# Поэтому текст рисуем ПОСИМВОЛЬНО: для каждого char берём первый шрифт
# из цепочки, у которого есть настоящий глиф (а не .notdef-заглушка).
_CHAIN_REGULAR_PATHS = (
    Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansMath-Regular.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)
_CHAIN_BOLD_PATHS = (
    Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    # Bold-варианта math-шрифта нет — regular сойдёт для фолбэка.
    Path("/usr/share/fonts/truetype/noto/NotoSansMath-Regular.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)
_chain_cache: dict = {}
_glyph_cache: dict = {}
_missing_sig_cache: dict = {}


def _chain_fonts(size: int, bold: bool):
    """Ordered font chain для (size, bold). Cached. Пустым не бывает."""
    key = (size, bold)
    if key not in _chain_cache:
        from PIL import ImageFont
        fonts = []
        for p in (_CHAIN_BOLD_PATHS if bold else _CHAIN_REGULAR_PATHS):
            if p.exists():
                try:
                    fonts.append(ImageFont.truetype(str(p), size=size))
                except Exception:
                    continue
        if not fonts:
            try:
                fonts.append(ImageFont.load_default())
            except Exception:
                pass
        _chain_cache[key] = fonts
    return _chain_cache[key]


def _missing_sig(font):
    """Сигнатура .notdef-заглушки шрифта (size + bbox маски U+10FFFF)."""
    fid = id(font)
    if fid not in _missing_sig_cache:
        try:
            m = font.getmask("\U0010FFFF")
            _missing_sig_cache[fid] = (m.size, m.getbbox())
        except Exception:
            _missing_sig_cache[fid] = None
    return _missing_sig_cache[fid]


def _font_has_glyph(font, ch: str) -> bool:
    """True если у шрифта настоящий глиф (не tofu). Cached по (font, ch)."""
    key = (id(font), ch)
    hit = _glyph_cache.get(key)
    if hit is None:
        try:
            m = font.getmask(ch)
            sig = (m.size, m.getbbox())
            hit = m.getbbox() is not None and sig != _missing_sig(font)
        except Exception:
            hit = False
        _glyph_cache[key] = hit
    return hit


def _font_for(ch: str, size: int, bold: bool):
    """Первый шрифт из цепочки с настоящим глифом. Fallback — базовый."""
    for f in _chain_fonts(size, bold):
        if _font_has_glyph(f, ch):
            return f
    fonts = _chain_fonts(size, bold)
    return fonts[0] if fonts else None


def _strip_html(text: str) -> str:
    """Strip HTML + unescape + collapse repeated spaces.

    Color emoji вырезаются ТОЛЬКО если нет emoji-шрифта (иначе их рисует
    _draw_runs). Коллапсим ТОЛЬКО repeated spaces (через
    ``re.sub(r" {2,}", " ", ...)``), НЕ \n — параграфы (\\n\\n) важны
    для multi-line сообщений в Telegram.
    """
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    if _emoji_base_font() is None:
        text = _EMOJI_RE.sub("", text)
    text = re.sub(r" {2,}", " ", text)
    text = text.replace("\u00a0", " ").strip()
    return text


def _emoji_base_font():
    """Noto Color Emoji в рабочем strike (109px). None если шрифта нет. Cached."""
    global _emoji_base
    if _emoji_base is None:
        try:
            from PIL import ImageFont
            if _EMOJI_FONT_PATH.exists():
                _emoji_base = ImageFont.truetype(str(_EMOJI_FONT_PATH), size=_EMOJI_STRIKE_SIZE)
            else:
                _emoji_base = False
        except Exception:
            _emoji_base = False
    return _emoji_base or None


def _segment_runs(text: str) -> list[tuple[str, str]]:
    """Делит строку на runs: ('t', обычный текст) и ('e', emoji-последовательность)."""
    runs: list[tuple[str, str]] = []
    pos = 0
    for m in _EMOJI_RUN_RE.finditer(text or ""):
        if m.start() > pos:
            runs.append(("t", text[pos:m.start()]))
        runs.append(("e", m.group(0)))
        pos = m.end()
    if pos < len(text or ""):
        runs.append(("t", (text or "")[pos:]))
    return runs


def _emoji_glyph(ch: str, px: int):
    """Один цветной глиф высотой ~px (RGBA). None если глифа нет (не tofu'им).

    Рендер в strike 109px + даунскейл. Без чернил (пустой bbox) → None.
    Cached по (ch, px).
    """
    key = (ch, px)
    if key in _emoji_glyph_cache:
        return _emoji_glyph_cache[key]
    out = None
    try:
        from PIL import Image, ImageDraw
        base = _emoji_base_font()
        if base is not None and ch not in _EMOJI_SKIP:
            cell = Image.new("RGBA", (_EMOJI_STRIKE_SIZE, _EMOJI_STRIKE_SIZE), (0, 0, 0, 0))
            d = ImageDraw.Draw(cell)
            d.text((0, 0), ch, font=base, embedded_color=True)
            bbox = cell.getbbox()
            if bbox:
                cell = cell.crop(bbox)
                h = max(1, int(px))
                w = max(1, int(cell.width * h / max(cell.height, 1)))
                out = cell.resize((w, h))
    except Exception:
        out = None
    _emoji_glyph_cache[key] = out
    return out


def _runs_width(runs: list[tuple[str, str]], font, emoji_px: int, bold: bool = False) -> float:
    """Ширина строки: текст — посимвольно через font-fallback chain, emoji — глифы."""
    size = getattr(font, "size", emoji_px) or emoji_px
    total = 0.0
    for kind, chunk in runs:
        if kind == "t":
            for ch in chunk:
                f = _font_for(ch, size, bold) or font
                try:
                    total += f.getlength(ch)
                except Exception:
                    total += 8
        else:
            for ch in chunk:
                g = _emoji_glyph(ch, emoji_px)
                if g is not None:
                    total += g.width
    return total


def _draw_runs(draw, img, x: float, y: float, text: str, font, emoji_px: int, fill, bold: bool = False) -> float:
    """Рисует строку: текст — посимвольно (font-fallback) + цветные emoji.

    Возвращает x конца. Baseline: глифы вписываются в высоту строки
    (emoji_px ≈ размер шрифта).
    """
    size = getattr(font, "size", emoji_px) or emoji_px
    cx = x
    for kind, chunk in _segment_runs(text):
        if kind == "t":
            for ch in chunk:
                f = _font_for(ch, size, bold) or font
                draw.text((cx, y), ch, fill=fill, font=f)
                try:
                    cx += f.getlength(ch)
                except Exception:
                    cx += 8
        else:
            for ch in chunk:
                g = _emoji_glyph(ch, emoji_px)
                if g is None:
                    continue
                try:
                    top = int(y + max(0, (font.size if hasattr(font, "size") else emoji_px) - g.height))
                    img.paste(g, (int(cx), top), mask=g)
                except Exception:
                    pass
                cx += g.width
    return cx


def _wrap_text(text: str, font, max_width: int, emoji_px: int | None = None, bold: bool = False) -> list[str]:
    """Word-wrap text → list of strings длиной до max_width pixels.

    Слова длиннее max_width бьются посимвольно — иначе строка
    уезжает за canvas. Замер всегда через runs (посимвольный
    font-fallback + цветные глифы), emoji_px нужен для высоты глифов.
    """
    if not text:
        return []

    px = emoji_px if emoji_px is not None else (getattr(font, "size", 24) or 24)

    def _measure(s: str) -> float:
        return _runs_width(_segment_runs(s), font, px, bold)

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

def _apply_background(img, bg_bytes: bytes):
    """Фото из реплая как фон: резкий fit-центр + блюр-подложка + затемнение.

    Подстраивается под размер цитаты (canvas): фото целиком по центру без
    кропа, пустые поля заливает блюр той же фотки. Pure (Pillow only).
    Возвращает новый RGB Image того же размера. Бросает исключение
    при битых байтах (caller ловит).
    """
    from PIL import Image, ImageFilter

    bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
    w, h = img.size
    # Подложка: cover-fit + blur (заполняет весь canvas без дыр).
    scale = max(w / max(bg.width, 1), h / max(bg.height, 1))
    cover = bg.resize((max(1, int(bg.width * scale)), max(1, int(bg.height * scale))))
    left = (cover.width - w) // 2
    top = (cover.height - h) // 2
    backdrop = cover.crop((left, top, left + w, top + h))
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(radius=14))
    # Центр: резкое фото целиком (contain-fit), без кропа.
    fit_scale = min(w / max(bg.width, 1), h / max(bg.height, 1))
    fw, fh = max(1, int(bg.width * fit_scale)), max(1, int(bg.height * fit_scale))
    sharp = bg.resize((fw, fh))
    backdrop.paste(sharp, ((w - fw) // 2, (h - fh) // 2))
    # Затемнение чтобы белый текст читался.
    dark = Image.new("RGB", (w, h), color=(0, 0, 0))
    return Image.blend(backdrop, dark, alpha=0.45)


def render_quote_png(
    *,
    body: str,
    sender_name: str = "",
    sender_id: int | None = None,
    usernames: list[str] | None = None,
    avatar_bytes: bytes | None = None,
    timestamp: str = "",
    background_bytes: bytes | None = None,
) -> bytes | None:
    """Render прямоугольной PNG-цитаты с avatar + bubble overlay.

    Args:
        body: Текст цитаты (plain text — без HTML).
        sender_name: Имя отправителя (first + last).
        sender_id: User ID (int).
        usernames: Список @username (cap 2 в layout). None или [] → нет.
        avatar_bytes: PNG/JPEG bytes аватарки. None → без аварки.
        timestamp: Строка даты (например, "2026-07-18 18:30") для info-блока.
        background_bytes: Фото из replied сообщения — используется как фон
            (cover-fit + blur + затемнение) вместо плоской заливки.

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

    # --- Wrap body (медиа-цитата без текста: bubble пропускаем) ---
    bubble_text_max_width = WIDTH - 2 * PADDING_X - 2 * BUBBLE_INSET_X - 2 * BUBBLE_TEXT_PAD_X
    body_lines = _wrap_text(body_text, body_font, max_width=bubble_text_max_width, emoji_px=BODY_FONT_SIZE)
    if len(body_lines) > MAX_BODY_LINES:
        body_lines = body_lines[:MAX_BODY_LINES]
        if body_lines:
            body_lines[-1] = body_lines[-1].rstrip() + "…"

    # --- Высота canvas (вертикаль × VERTICAL_SCALE, текст центрируем в строках) ---
    _pad_y = int(PADDING_Y * VERTICAL_SCALE)
    _name_lh = int(NAME_LINE_HEIGHT * VERTICAL_SCALE)
    _info_lh = int(INFO_LINE_HEIGHT * VERTICAL_SCALE)
    _body_lh = int(BODY_LINE_HEIGHT * VERTICAL_SCALE)
    _gap = int(HEADER_TO_BUBBLE_GAP * VERTICAL_SCALE)
    _bub_pad_y = int(BUBBLE_TEXT_PAD_Y * VERTICAL_SCALE)
    _min_h = int(MIN_HEIGHT * VERTICAL_SCALE)
    _max_h = int(MAX_HEIGHT * VERTICAL_SCALE)
    header_text_height = _name_lh + _info_lh + 6
    header_height = max(AVATAR_SIZE, header_text_height)
    if body_lines:
        body_block_h = len(body_lines) * _body_lh
        bubble_height = body_block_h + 2 * _bub_pad_y
        total_height = (
            _pad_y + header_height
            + _gap
            + bubble_height
            + _pad_y
        )
    else:
        # Медиа-цитата без текста: только header.
        total_height = _pad_y + header_height + _pad_y
    total_height = min(_max_h, max(_min_h, total_height))

    # --- Canvas: RGB (universal preview) ---
    img = Image.new("RGB", (WIDTH, total_height), color=BG_COLOR)
    if background_bytes:
        try:
            img = _apply_background(img, background_bytes)
        except Exception:
            pass

    # --- Header: avatar + name + info ---
    avatar_x = PADDING_X
    avatar_y = _pad_y + (header_height - AVATAR_SIZE) // 2 if avatar_img else _pad_y
    text_x = PADDING_X
    if avatar_img is not None:
        try:
            img.paste(avatar_img, (avatar_x, avatar_y), mask=avatar_img.split()[3])
        except Exception:
            img.paste(avatar_img.convert("RGB"), (avatar_x, avatar_y))
        text_x = avatar_x + AVATAR_SIZE + 20

    name_y = _pad_y + (header_height - header_text_height) // 2
    draw = ImageDraw.Draw(img)
    # Центрируем глифы внутри (вытянутых) строк — иначе текст липнет к верху.
    _draw_runs(draw, img, text_x, name_y + (_name_lh - NAME_FONT_SIZE) // 2,
               sender_name_text, name_font, NAME_FONT_SIZE, NAME_COLOR, bold=True)
    if info_str:
        info_y = name_y + _name_lh + 2
        _draw_runs(draw, img, text_x, info_y + (_info_lh - INFO_FONT_SIZE) // 2,
                   info_str, info_font, INFO_FONT_SIZE, INFO_COLOR)

    # --- Semi-transparent bubble overlay (RGBA) + body (только если есть текст) ---
    if body_lines:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        bubble_y_top = _pad_y + header_height + _gap
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
    if body_lines:
        draw = ImageDraw.Draw(img)
        text_y = bubble_y_top + _bub_pad_y + (_body_lh - BODY_FONT_SIZE) // 2
        text_x = PADDING_X + BUBBLE_INSET_X + BUBBLE_TEXT_PAD_X
        for line in body_lines:
            _draw_runs(draw, img, text_x, text_y, line, body_font, BODY_FONT_SIZE, TEXT_COLOR)
            text_y += _body_lh

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


MAX_QUOTE_GIF_BYTES = 12 * 1024 * 1024


def _stack_gif_over_card(gif_bytes: bytes, card_png: bytes, max_frames: int = 30) -> bytes | None:
    """Кадры GIF + статичная карточка цитаты снизу. Pure (Pillow) — покрыто тестами."""
    from PIL import Image, ImageSequence

    gif = Image.open(io.BytesIO(gif_bytes))
    card = Image.open(io.BytesIO(card_png)).convert("RGB")
    frames: list = []
    durations: list[int] = []
    for i, frame in enumerate(ImageSequence.Iterator(gif)):
        if i >= max_frames:
            break
        fr = frame.convert("RGB")
        w, h = fr.size
        card_h = max(1, int(card.height * w / max(card.width, 1)))
        card_small = card.resize((w, card_h))
        canvas = Image.new("RGB", (w, h + card_h), color=(0, 0, 0))
        canvas.paste(fr, (0, 0))
        canvas.paste(card_small, (0, h))
        frames.append(canvas)
        durations.append(int(frame.info.get("duration", 100)) or 100)
    if len(frames) < 2:
        # Один кадр — не анимация, такой «GIF» бесполезен.
        return None
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations, loop=0,
    )
    out = buf.getvalue()
    if len(out) > MAX_QUOTE_GIF_BYTES:
        return None
    return out


async def render_video_quote_gif(
    video_bytes: bytes,
    *,
    card_png_bytes: bytes,
    max_width: int = 640,
    max_frames: int = 30,
    fps: int = 10,
    duration_s: float = 3.0,
    timeout_s: float = 60.0,
) -> bytes | None:
    """Видео из реплая + карточка цитаты снизу → анимированная GIF.

    Returns None если нет ffmpeg / encode упал / итог тяжелее лимита —
    caller падает на статичную PNG-цитату.
    """
    if not video_bytes or not card_png_bytes:
        return None
    try:
        from utils.gif_converter import video_to_gif_bytes
        gif = await video_to_gif_bytes(
            video_bytes,
            max_duration_s=duration_s,
            max_fps=fps,
            max_width=max_width,
            timeout_s=timeout_s,
        )
    except Exception:
        return None
    if not gif:
        return None
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _stack_gif_over_card, gif, card_png_bytes, max_frames)
    except Exception:
        return None
