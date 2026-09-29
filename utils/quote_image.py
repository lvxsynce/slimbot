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

# Вертикальный масштаб карточки: применяется к отступам и гэпам (PADDING_Y,
# HEADER_TO_BUBBLE_GAP, BUBBLE_TEXT_PAD_Y). Межстрочные интервалы НЕ
# масштабируются — строки текста идут компактно, без «переносов» между ними.
# Ширина и размер шрифтов не меняются.
VERTICAL_SCALE = 2.5

# Two-column layout: слева инфо-колонка, справа контент-бокс.
LEFT_COL_W = 460
LEFT_COL_PAD = 16
COLUMN_GAP = 40
PHOTO_MAX_H = 1000   # кап высоты фото в правой колонке
STRIP_WIDTH = 440    # ширина info-strip для GIF-цитат

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
    "\u2190-\u21FF"
    "\u25A0-\u25FF"
    "\u2B00-\u2BFF"
    "\u20E3"
    "\u200d"
    "\ufe0f"
    "]+"
    "|[0-9#*]\ufe0f?\u20E3",
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


# ===== Custom (анимированные Premium) эмодзи =====
# Telegram-кастомные эмодзи — это отдельные документы (TGS/Lottie или
# WEBM/VP9+alpha), а НЕ символы Unicode. В тексте они лежат как
# `MessageEntityCustomEmoji(document_id=...)`, а сам символ-заглушка —
# стандартный Unicode-эмодзи (🔥 и т.п.).
#
# В `handle()` диапазоны этих entity заменяются на PUA-маркеры
# (U+E000…), и сюда передаётся словарь `{маркер: RGBA-кадр}`. Если маркер
# не скачался (нет rlottie / битый файл / offline) — маркер НЕ рисуется
# вообще, и мы НЕ рисуем tofu: PUA-диапазон не имеет глифа ни в одном
# шрифте цепочки, поэтому молча пропускаем символ.
def _is_pua(ch: str) -> bool:
    """Private-use маркер custom emoji (U+E000–U+F8FF)."""
    return len(ch) == 1 and 0xE000 <= ord(ch) <= 0xF8FF


def _inline_advance(im, px: int) -> int:
    """Ширина inline-картинки при высоте px (aspect preserved)."""
    try:
        return max(1, int(im.width * max(1, int(px)) / max(im.height, 1)))
    except Exception:
        return int(px)


def _inline_frame_for(inline: dict, ch: str):
    """Кадр custom-эмодзи для маркера. None если маркера нет.

    Значение в `inline` — либо один PIL.Image (статика), либо список
    кадров (анимация; для статичного PNG берём первый).
    """
    im = inline.get(ch)
    if im is None:
        return None
    if isinstance(im, (list, tuple)):
        return im[0] if im else None
    return im


def _runs_width(runs: list[tuple[str, str]], font, emoji_px: int, bold: bool = False,
                inline: dict | None = None) -> float:
    """Ширина строки: inline-картинки, emoji-глифы, текст через font-fallback."""
    size = getattr(font, "size", emoji_px) or emoji_px
    total = 0.0
    for kind, chunk in runs:
        if kind == "t":
            for ch in chunk:
                if inline:
                    im = _inline_frame_for(inline, ch)
                    if im is not None:
                        total += _inline_advance(im, emoji_px)
                        continue
                if _is_pua(ch):
                    # Маркер без картинки — молча пропускаем, не tofu.
                    # Проверка ВНЕ `if inline`: PUA-символ не имеет глифа
                    # ни в одном шрифте цепочки, поэтому без этой строки он
                    # превратится в пустой прямоугольник.
                    continue
                if ch not in _EMOJI_SKIP and _EMOJI_RUN_RE.match(ch):
                    g = _emoji_glyph(ch, emoji_px)
                    if g is not None:
                        total += g.width
                        continue
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


def _draw_runs(draw, img, x: float, y: float, text: str, font, emoji_px: int, fill,
               bold: bool = False, inline: dict | None = None) -> float:
    """Рисует строку: inline-картинки + цветные emoji + текст (font-fallback).

    Возвращает x конца. Baseline: глифы вписываются в высоту строки
    (emoji_px ≈ размер шрифта).
    """
    size = getattr(font, "size", emoji_px) or emoji_px
    cx = x
    for kind, chunk in _segment_runs(text):
        if kind == "t":
            for ch in chunk:
                if inline:
                    im = _inline_frame_for(inline, ch)
                    if im is not None:
                        h = max(1, int(emoji_px))
                        w = _inline_advance(im, h)
                        r = im if (im.width == w and im.height == h) else im.resize((w, h))
                        top = int(y + max(0, (font.size if hasattr(font, "size") else emoji_px) - h))
                        if r.mode in ("RGBA", "LA"):
                            img.paste(r, (int(cx), top), mask=r.split()[-1])
                        else:
                            img.paste(r, (int(cx), top))
                        cx += w
                        continue
                if _is_pua(ch):
                    # ВНЕ `if inline` — см. комментарий в _runs_width.
                    continue
                if ch not in _EMOJI_SKIP and _EMOJI_RUN_RE.match(ch):
                    g = _emoji_glyph(ch, emoji_px)
                    if g is not None:
                        try:
                            top = int(y + max(0, (font.size if hasattr(font, "size") else emoji_px) - g.height))
                            img.paste(g, (int(cx), top), mask=g)
                        except Exception:
                            pass
                        cx += g.width
                        continue
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


def _wrap_text(text: str, font, max_width: int, emoji_px: int | None = None, bold: bool = False,
               inline: dict | None = None) -> list[str]:
    """Word-wrap text → list of strings длиной до max_width pixels.

    Слова длиннее max_width бьются посимвольно — иначе строка
    уезжает за canvas. Замер всегда через runs (посимвольный
    font-fallback + цветные глифы), emoji_px нужен для высоты глифов.
    """
    if not text:
        return []

    px = emoji_px if emoji_px is not None else (getattr(font, "size", 24) or 24)

    def _measure(s: str) -> float:
        return _runs_width(_segment_runs(s), font, px, bold, inline)

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


def _circle_avatar(avatar_bytes: bytes, size: int = AVATAR_SIZE):
    """Return PIL RGBA Image of circular-cropped avatar, or None on failure."""
    from PIL import Image, ImageDraw
    try:
        size = max(16, int(size))
        av = Image.open(io.BytesIO(avatar_bytes))
        av = av.convert("RGBA").resize(
            (size, size), Image.Resampling.LANCZOS
        )
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse(
            (0, 0, size, size), fill=255
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
    inline_images: dict | None = None,
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
        inline_images: ``{PUA-маркер: RGBA Image | [RGBA Image, ...]}`` —
            кастомные (анимированные) эмодзи. Для статичного PNG берётся
            первый кадр; анимация живёт в GIF-ветке (`anim_strips`).

    Returns:
        PNG bytes (RGB, не RGBA — для universal preview в Telegram).
        ``None`` если Pillow/fonts недоступны.

    Layout (две колонки):
        ┌─────────────────────────────────┐
        │ [avatar]  │ ┌─────────────────┐ │
        │ Sender    │ │ «body text...»  │ │  ← контент-бокс справа
        │ ID: 123   │ │ more text...»   │ │    (фото + текст в кавычках)
        │ @user · t │ └─────────────────┘ │
        └─────────────────────────────────┘
      Слева вверху — вся информация (аватар, имя, ID, username, дата).
      Справа — вложение (фото fit-вписанное) и/или текст.
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

    # --- Вертикальный масштаб ---
    _pad_y = int(PADDING_Y * VERTICAL_SCALE)
    _name_lh = NAME_LINE_HEIGHT
    _info_lh = INFO_LINE_HEIGHT
    _body_lh = BODY_LINE_HEIGHT
    _gap = int(HEADER_TO_BUBBLE_GAP * VERTICAL_SCALE)
    _bub_pad_y = int(BUBBLE_TEXT_PAD_Y * VERTICAL_SCALE)
    _min_h = int(MIN_HEIGHT * VERTICAL_SCALE)
    _max_h = int(MAX_HEIGHT * VERTICAL_SCALE)

    # --- Левая колонка: avatar + имя + ID + username + дата ---
    left_text_w = LEFT_COL_W - 2 * LEFT_COL_PAD
    name_lines = (
        _wrap_text(sender_name_text, name_font, max_width=left_text_w,
                   emoji_px=NAME_FONT_SIZE, bold=True)
        or [sender_name_text]
    )
    info_items: list[str] = []
    if sender_id:
        info_items.append(f"ID: {sender_id}")
    for u in unames:
        info_items.append(f"@{u}")
    if timestamp_text:
        info_items.append(timestamp_text)
    info_lines: list[str] = []
    for item in info_items:
        info_lines.extend(
            _wrap_text(item, info_font, max_width=left_text_w,
                       emoji_px=INFO_FONT_SIZE) or [item]
        )
    left_h = 0
    if avatar_img is not None:
        left_h += AVATAR_SIZE + _gap // 2
    left_h += len(name_lines) * _name_lh + _gap // 2 + len(info_lines) * _info_lh

    # --- Правая колонка: фото (вложение) + текст в «кавычках» ---
    right_x = PADDING_X + LEFT_COL_W + COLUMN_GAP
    right_w = WIDTH - PADDING_X - right_x
    right_text_w = right_w - 2 * BUBBLE_TEXT_PAD_X
    quoted = body_text
    body_lines = _wrap_text(quoted, body_font, max_width=right_text_w,
                            emoji_px=BODY_FONT_SIZE, inline=inline_images)
    if len(body_lines) > MAX_BODY_LINES:
        body_lines = body_lines[:MAX_BODY_LINES]
        if body_lines:
            body_lines[-1] = body_lines[-1].rstrip() + "…"
    photo_img = None
    if background_bytes:
        try:
            from PIL import Image
            ph = Image.open(io.BytesIO(background_bytes)).convert("RGB")
            scale = min(right_text_w / max(ph.width, 1), PHOTO_MAX_H / max(ph.height, 1))
            scale = min(max(scale, 0.05), 3.0)
            photo_img = ph.resize((max(1, int(ph.width * scale)), max(1, int(ph.height * scale))))
        except Exception:
            photo_img = None
    right_h = 0
    if photo_img is not None or body_lines:
        if photo_img is not None:
            right_h += photo_img.height
            if body_lines:
                right_h += _gap // 2
        right_h += len(body_lines) * _body_lh
        right_h += 2 * _bub_pad_y

    content_h = max(left_h, right_h)
    total_height = min(_max_h, max(_min_h, 2 * _pad_y + content_h))

    # --- Canvas: RGB (universal preview) ---
    img = Image.new("RGB", (WIDTH, total_height), color=BG_COLOR)

    # --- Левая колонка ---
    draw = ImageDraw.Draw(img)
    lx = PADDING_X + LEFT_COL_PAD
    ly = _pad_y
    if avatar_img is not None:
        try:
            img.paste(avatar_img, (lx, ly), mask=avatar_img.split()[3])
        except Exception:
            img.paste(avatar_img.convert("RGB"), (lx, ly))
        ly += AVATAR_SIZE + _gap // 2
    for line in name_lines:
        _draw_runs(draw, img, lx, ly + (_name_lh - NAME_FONT_SIZE) // 2,
                   line, name_font, NAME_FONT_SIZE, NAME_COLOR, bold=True)
        ly += _name_lh
    ly += _gap // 2
    for line in info_lines:
        _draw_runs(draw, img, lx, ly + (_info_lh - INFO_FONT_SIZE) // 2,
                   line, info_font, INFO_FONT_SIZE, INFO_COLOR)
        ly += _info_lh

    # --- Правый бокс: фон на всю правую часть + фото + текст ---
    if right_h > 0:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        bubble_box = (right_x, _pad_y, WIDTH - PADDING_X, total_height - _pad_y)
        overlay_draw.rounded_rectangle(bubble_box, radius=16, fill=BUBBLE_COLOR)
        img.paste(overlay, (0, 0), mask=overlay)
        draw = ImageDraw.Draw(img)
        cy = _pad_y + _bub_pad_y
        if photo_img is not None:
            px = right_x + (right_w - photo_img.width) // 2
            img.paste(photo_img, (px, cy))
            draw.rectangle(
                (px, cy, px + photo_img.width, cy + photo_img.height),
                outline=INFO_COLOR, width=2,
            )
            cy += photo_img.height
            if body_lines:
                cy += _gap // 2
        tx = right_x + BUBBLE_TEXT_PAD_X
        for line in body_lines:
            _draw_runs(draw, img, tx, cy + (_body_lh - BODY_FONT_SIZE) // 2,
                       line, body_font, BODY_FONT_SIZE, TEXT_COLOR,
                       inline=inline_images)
            cy += _body_lh

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


MAX_QUOTE_GIF_BYTES = 12 * 1024 * 1024


def render_info_strip_png(
    *,
    sender_name: str = "",
    sender_id: int | None = None,
    usernames: list[str] | None = None,
    avatar_bytes: bytes | None = None,
    timestamp: str = "",
    body: str = "",
    width: int = STRIP_WIDTH,
    inline_images: dict | None = None,
    scale: float = 1.0,
) -> bytes | None:
    """Узкая вертикальная info-плашка для GIF-цитат: слева от кадров.

    Содержит avatar + имя + ID + username + дату + опциональный текст
    (подпись к видео/гифке). Высоту подгоняет `_stack_gif_side_by_side`
    под высоту видеокадра.

    ``inline_images`` — те же PUA-маркеры, что и в `render_quote_png`;
    для GIF-цитаты рендерится один кадр на фазу анимации (см. anim_strips).

    ``scale`` — изотропный множитель разрешения (шрифты, аватар, отступы,
    line-height). Плашка потом масштабируется под высоту кадра, поэтому её
    выгодно рисовать с запасом: даунскейл текста чёткий, апскейл — каша.
    ВАЖНО: масштабировать нужно ОБЕ оси — иначе плашка получит неверную
    пропорцию (например 880×334 вместо 880×668) и в раскладке
    `_gif_side_layout` «съест» вдвое больше ширины холста.
    """
    if not is_available():
        return None

    from PIL import Image, ImageDraw

    sc = max(0.5, min(4.0, float(scale or 1.0)))

    sender_name_text = _strip_html(sender_name) or "(unknown)"
    unames = (usernames or [])[:2]
    timestamp_text = _strip_html(timestamp)
    body_text = _strip_html(body)

    name_px = max(8, int(NAME_FONT_SIZE * sc))
    info_px = max(8, int(INFO_FONT_SIZE * sc))
    body_px = max(8, int(BODY_FONT_SIZE * sc))
    name_font = _resolve_font(name_px, sender_name_text)
    info_font = _resolve_font(info_px, "ID")
    body_font = _resolve_font(body_px, body_text)
    if not (name_font and info_font and body_font):
        return None

    pad = max(8, int(36 * sc))
    avatar_size = max(24, int(AVATAR_SIZE * sc))
    text_w = width - 2 * pad
    gap = int(HEADER_TO_BUBBLE_GAP * VERTICAL_SCALE * sc)
    name_lh = max(1, int(NAME_LINE_HEIGHT * sc))
    info_lh = max(1, int(INFO_LINE_HEIGHT * sc))
    body_lh = max(1, int(BODY_LINE_HEIGHT * sc))

    avatar_img = _circle_avatar(avatar_bytes, size=avatar_size) if avatar_bytes else None

    name_lines = (
        _wrap_text(sender_name_text, name_font, max_width=text_w,
                   emoji_px=name_px, bold=True)
        or [sender_name_text]
    )
    info_items: list[str] = []
    if sender_id:
        info_items.append(f"ID: {sender_id}")
    for u in unames:
        info_items.append(f"@{u}")
    if timestamp_text:
        info_items.append(timestamp_text)
    info_lines: list[str] = []
    for item in info_items:
        info_lines.extend(
            _wrap_text(item, info_font, max_width=text_w,
                       emoji_px=info_px) or [item]
        )
    quoted = body_text
    body_lines = _wrap_text(quoted, body_font, max_width=text_w,
                            emoji_px=body_px, inline=inline_images)
    if len(body_lines) > MAX_BODY_LINES:
        body_lines = body_lines[:MAX_BODY_LINES]
        if body_lines:
            body_lines[-1] = body_lines[-1].rstrip() + "…"

    h = pad
    if avatar_img is not None:
        h += avatar_size + gap // 2
    h += len(name_lines) * name_lh + gap // 2 + len(info_lines) * info_lh
    if body_lines:
        h += gap // 2 + len(body_lines) * body_lh
    h += pad

    img = Image.new("RGB", (width, max(h, int(320 * sc))), color=BG_COLOR)
    draw = ImageDraw.Draw(img)
    y = pad
    if avatar_img is not None:
        try:
            img.paste(avatar_img, (pad, y), mask=avatar_img.split()[3])
        except Exception:
            img.paste(avatar_img.convert("RGB"), (pad, y))
        y += avatar_size + gap // 2
    for line in name_lines:
        _draw_runs(draw, img, pad, y + (name_lh - name_px) // 2,
                   line, name_font, name_px, NAME_COLOR, bold=True,
                   inline=inline_images)
        y += name_lh
    y += gap // 2
    for line in info_lines:
        _draw_runs(draw, img, pad, y + (info_lh - info_px) // 2,
                   line, info_font, info_px, INFO_COLOR,
                   inline=inline_images)
        y += info_lh
    if body_lines:
        y += gap // 2
        for line in body_lines:
            _draw_runs(draw, img, pad, y + (body_lh - body_px) // 2,
                       line, body_font, body_px, TEXT_COLOR,
                       inline=inline_images)
            y += body_lh

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _as_gif_direct(data: bytes) -> bytes | None:
    """Если bytes уже GIF с ≥2 кадрами — вернуть как есть (без ffmpeg).

    Это фиксит пустые GIF-цитаты: реплай-гифка раньше всегда гналась через
    ffmpeg (которого на сервере не было) и путь молча падал в статичный PNG.
    """
    if not data:
        return None
    try:
        from PIL import Image
        gif = Image.open(io.BytesIO(data))
        if (gif.format or "").upper() != "GIF":
            return None
        n = getattr(gif, "n_frames", 0) or 0
        if n < 2:
            try:
                from PIL import ImageSequence
                n = sum(1 for _ in ImageSequence.Iterator(gif))
            except Exception:
                return None
        return data if n >= 2 else None
    except Exception:
        return None


# Геометрия GIF-цитаты. Плашка занимает фиксированную долю ширины холста и
# центрируется по вертикали; остальное место — под кадр. Так кружок 200×200
# растягивается на ~60% ширины цитаты (раньше жёсткий лимит «не более 2x»
# оставлял его 400px в холсте 926px — цитата выглядела мелкой и «чёрной»,
# т.к. круг занимал меньше половины кадра).
GIF_STRIP_SHARE = 0.40      # доля ширины холста под info-плашку
GIF_MAX_UPSCALE = 3.5       # больше не растим: 200px кружок → каша
GIF_MAX_HEIGHT = 820       # потолок высоты холста (иначе портреты огромны)
# МЯГКИЙ потолок веса. Жёсткий лимит — MAX_QUOTE_GIF_BYTES (12МБ), но держаться
# впритык к нему опасно: любое чуть более шумное видео его пробивает, и цитата
# молча уходит на статичную карточку с заглушкой. Реальный кейс: видео-цитата
# весила 12.2МБ из 12.6МБ — 97% лимита.
GIF_SOFT_BYTES = 6 * 1024 * 1024
GIF_MIN_HEIGHT = 200
# Потолок длительности одного цикла GIF-цитаты. Кружки бывают на 60 с;
# без потолка GIF крутился бы минуту, поэтому длинные ролики прореживаются
# сильнее (но остаются в пределах разумного).
GIF_MAX_LOOP_MS = 20_000
# Сколько из 256 цветов палитры резервируем под info-плашку. Без резерва
# насыщенное видео (кружок с зелёно-оранжевым фоном) забирало всю палитру,
# и серо-синий текст NAME_COLOR/INFO_COLOR маппился на оранжевый.
GIF_STRIP_PALETTE_COLORS = 64


def _split_palette(canvases: list, strip_w: int, strip_colors: int) -> Any:
    """Палитра GIF: первые N цветов — из info-плашки, остальные — из видео.

    Без разделения MEDIANCUT по общему монтажу отдавал все 256 цветов
    насыщенному кадру, и текст цитаты менял цвет на глазах у юзера.
    Плашка статична, поэтому её палитру можно построить один раз и
    зафиксировать; видео дизерится в оставшиеся слоты.
    """
    from PIL import Image
    n_strip = max(8, min(200, int(strip_colors)))
    n_video = 256 - n_strip

    # 1) Палитра плашки — по её собственному куску из первого кадра.
    strip_sample = canvases[0].crop((0, 0, max(1, strip_w), canvases[0].height))
    strip_pal = strip_sample.quantize(colors=n_strip, method=Image.MEDIANCUT)
    strip_rgb = (strip_pal.getpalette() or [])[: n_strip * 3]

    # 2) Палитра видео — по монтажу тамбнейлов (иначе мерцание).
    thumbs = []
    for c in canvases:
        t = c.crop((strip_w, 0, c.width, c.height))
        if t.width < 1 or t.height < 1:
            t = c.copy()
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
    video_pal = montage.quantize(colors=n_video, method=Image.MEDIANCUT)
    video_rgb = (video_pal.getpalette() or [])[: n_video * 3]

    # 3) Склеиваем: слоты плашки фиксированы, хвост добирает видео.
    palette = list(strip_rgb)
    palette += list(video_rgb)
    palette = palette[: 256 * 3]
    palette += [0] * (256 * 3 - len(palette))
    combined = Image.new("P", (1, 1))
    combined.putpalette(palette)
    return combined


def _gif_side_layout(video_size, strip_size, target_width: int,
                     max_upscale: float, max_width: int) -> dict:
    """Раскладка кадра и плашки для GIF-цитаты. Pure.

    Плашка занимает левую часть и **растягивается на всю высоту холста**
    (иначе она «висит» узкой полосой в чёрном поле). Высота холста
    подбирается так, чтобы суммарная ширина вышла на ``target_width``:

        H * (sw0/sh0 + vw0/vh0) = target_width

    откуда ``H = target_width / (sw0/sh0 + vw0/vh0)``. Далее апскейл кадра
    ограничен ``max_upscale`` — иначе кружок 200×200 превратится в кашу.

    Возвращает ``{'video': (w,h), 'strip_w', 'strip_h', 'strip_y', 'canvas'}``.
    """
    vw0, vh0 = video_size
    sw0, sh0 = strip_size
    vw0, vh0 = max(1, int(vw0)), max(1, int(vh0))
    sw0, sh0 = max(1, int(sw0)), max(1, int(sh0))
    target_width = max(320, int(target_width))

    strip_ratio = sw0 / sh0
    video_ratio = vw0 / vh0
    denom = strip_ratio + video_ratio
    H = int(target_width / denom) if denom > 0 else vh0
    # Апскейл кадра не выше max_upscale (200px кружок → 700px, не 1400).
    H = min(H, int(vh0 * max_upscale))
    H = max(H, GIF_MIN_HEIGHT)
    # Потолок высоты холста (иначе портретное видео даёт 3000px по высоте).
    if H > GIF_MAX_HEIGHT:
        H = GIF_MAX_HEIGHT
    # Ширина кадра по построению ≤ target_width (H выведен из неё), поэтому
    # отдельного клампа по max_width здесь не нужно: max_upscale и
    # GIF_MAX_HEIGHT уже ограничивают раздувание.

    video_w = max(1, int(video_ratio * H))
    strip_w = max(1, int(strip_ratio * H))
    return {
        "video": (video_w, H),
        "strip_w": strip_w,
        "strip_h": H,
        "strip_y": 0,
        "canvas": (strip_w + video_w, H),
    }


def _sample_plan(total: int, max_frames: int) -> tuple[set | None, float]:
    """(индексы_кадров, шаг_прореживания) для выборки из total кадров.

    Равномерная выборка вместо «первых N» — иначе длинное видео (кружок до
    60 с) показывается только с начала. Возвращает ``(None, 1.0)``, если
    прореживать не нужно.

    Шаг отдаётся, чтобы длительности кадров домножить на него: иначе
    прореженная GIF играет в разы быстрее оригинала (30-секундный кружок
    прокрутился бы за 13 с).
    """
    if total <= 0 or total <= max_frames:
        return None, 1.0
    k = max(2, max_frames)
    idxs = {round(i * (total - 1) / (k - 1)) for i in range(k)}
    return idxs, total / max(1, len(idxs))


def _stack_gif_side_by_side(gif_bytes: bytes, strip_png: bytes, max_frames: int = 40,
                            max_width: int = 800, anim_strips: list | None = None,
                            anim_duration_ms: int = 0, circle: bool = False,
                            target_width: int | None = None) -> bytes | None:
    """Публичная обёртка: при превышении лимита — деградация по плану.

    GIF-цитата кружка может весить несколько МБ (дизеринг + 700px кадр), и на
    «шумном» клипе лимит 12MB пробивался — раньше это молча уводило цитату на
    статичную карточку с заглушкой «Видеосообщение».

    Порядок деградации важен. Сначала режем число кадров, но НЕ до конца:
    на насыщенном клипе так доходило до 2 кадров, то есть не анимация.
    Поэтому дальше уменьшаем размер холста, сохраняя и кадры, и движение —
    для кружка это куда приятнее, чем два неподвижных кадра.
    """
    plan = (
        (max_frames, 1.00),
        (max_frames // 2, 1.00),
        (max_frames // 2, 0.85),
        (max(4, max_frames // 3), 0.70),
        (max(4, max_frames // 4), 0.60),
        (max(4, max_frames // 5), 0.50),
        (max(3, max_frames // 8), 0.40),
    )
    seen: set = set()
    for frames, wscale in plan:
        if frames < 2 or (frames, wscale) in seen:
            continue
        seen.add((frames, wscale))
        tw = None
        if wscale != 1.0:
            tw = int((target_width or WIDTH) * wscale)
        out = _stack_gif_once(
            gif_bytes, strip_png, frames, max_width,
            anim_strips, anim_duration_ms, circle, tw,
        )
        if out is not None:
            if (frames, wscale) != (max_frames, 1.00):
                logger.debug(
                    "quote gif: в лимит уложились за счёт frames=%s width×%s",
                    frames, wscale)
            return out
    return None


def _stack_gif_once(gif_bytes: bytes, strip_png: bytes, max_frames: int = 40,
                            max_width: int = 800, anim_strips: list | None = None,
                            anim_duration_ms: int = 0, circle: bool = False,
                            target_width: int | None = None) -> bytes | None:
    """Кадры GIF справа + info-плашка слева. Pure (Pillow) — покрыто тестами.

    Геометрия (важно для кружков):
    - размер кадра подбирается так, чтобы ШИРИНА холста вышла на
      ``target_width`` (по умолчанию WIDTH=1400, как у PNG-карточки) —
      иначе GIF-цитата кружка выходит ~900px и в чате выглядит мелкой;
    - апскейл ограничен ``max_upscale`` (3.5x): кружок в Telegram 200×200,
      раздувать его сильнее — каша, но и оставлять 2× (как раньше) — тоже
      мало: круг занимал лишь треть ширины цитаты;
    - если исходник крупный — только ужимаем до target_width, не растим.

    Качество:
    - ОДНА общая 256-палитра на все кадры (MEDIANCUT по монтаж-тамбнейлам +
      FLOYDSTEINBERG): без этого покадровые палитры дают грязь и мерцание.

    ``anim_strips`` / ``anim_duration_ms`` — анимация custom-эмодзи: список
    info-плашек по фазам (равномерно по циклу эмодзи) и полная длительность
    цикла в мс. Каждый кадр берёт фазу по накопленному времени, поэтому
    эмодзи в тексте анимируется синхронно с гифкой справа.

    ``circle`` — наложить круглую альфа-маску (для кружков video_note:
    Telegram отдаёт их квадратом и рисует круг в клиенте).
    """
    from PIL import Image, ImageSequence

    gif = Image.open(io.BytesIO(gif_bytes))
    base_strip = Image.open(io.BytesIO(strip_png)).convert("RGB")
    layout = None
    phase_strips: list = []
    if anim_strips and anim_duration_ms > 0:
        for s in anim_strips:
            try:
                phase_strips.append(Image.open(io.BytesIO(s)).convert("RGB"))
            except Exception:
                pass
        if not phase_strips:
            anim_duration_ms = 0
    canvases: list = []
    durations: list[int] = []
    t_cum = 0
    # Кадры > max_frames берём РАВНОМЕРНО по всей длительности, а не первые N.
    # Иначе 60-секундный кружок показывал только первые ~3 секунды
    # («берётся только начало»), хотя длительность цитаты это позволяла.
    try:
        _total = int(getattr(gif, "n_frames", 0) or 0)
    except Exception:
        _total = 0
    wanted, stride = _sample_plan(_total, max_frames)
    for i, frame in enumerate(ImageSequence.Iterator(gif)):
        if wanted is not None and i not in wanted:
            continue
        dur = int(frame.info.get("duration", 100)) or 100
        strip = base_strip
        if phase_strips:
            ph = int((t_cum % anim_duration_ms) / anim_duration_ms * len(phase_strips))
            strip = phase_strips[min(ph, len(phase_strips) - 1)]
        t_cum += dur
        fr = frame.convert("RGB")
        if circle:
            # Круглая маска применяется ДО апскейла: маска строится под
            # исходный размер кадра, поэтому её не нужно строить на 700px.
            try:
                from utils.gif_converter import apply_circle_mask
                masked = apply_circle_mask(fr)
                if masked is not None:
                    # Кадр оставляем RGBA, чтобы углы были прозрачными; фон
                    # под ними — тёмный, как у полосы слева.
                    fr = masked
            except Exception as e:
                logger.debug(f"_stack_gif_side_by_side: circle mask failed: {e}")
                fr = frame.convert("RGB")
        if layout is None:
            # Геометрия считается один раз по первому кадру: кадры одного
            # клипа одной формы, а пересчёт в каждом кадре — лишняя работа.
            layout = _gif_side_layout(
                fr.size, base_strip.size,
                target_width or WIDTH, GIF_MAX_UPSCALE, max_width,
            )
        w, h = layout["video"]
        if fr.size != (w, h):
            fr = fr.resize((w, h), Image.LANCZOS)
        strip_w = layout["strip_w"]
        strip_small = strip.resize((strip_w, layout["strip_h"]), Image.LANCZOS)
        strip_y = layout["strip_y"]
        if fr.mode in ("RGBA", "LA"):
            # Композит через альфа: круг вырезан, углы = цвет плашки/фона.
            # Холст обязан быть шириной strip_w + w, иначе alpha_composite
            # молча обрежет кадр по правому краю (все кадры станут идентичными).
            canvas = Image.new("RGBA", (strip_w + w, h), color=(0, 0, 0, 255))
            canvas.paste(strip_small, (0, strip_y))
            canvas.alpha_composite(fr, (strip_w, 0))
            canvas = canvas.convert("RGB")
        else:
            canvas = Image.new("RGB", (strip_w + w, h), color=(0, 0, 0))
            canvas.paste(strip_small, (0, strip_y))
            canvas.paste(fr, (strip_w, 0))
        canvases.append(canvas)
        durations.append(dur)
    if len(canvases) < 2:
        return None
    # Длительности домножаем на шаг прореживания → цитата играет в реальном
    # темпе. Потолок цикла: 60-секундный кружок не должен крутиться минуту.
    if stride > 1.01:
        durations = [max(20, int(d * stride)) for d in durations]
    total_ms = sum(durations)
    if total_ms > GIF_MAX_LOOP_MS:
        k = GIF_MAX_LOOP_MS / float(total_ms)
        durations = [max(20, int(d * k)) for d in durations]
    palette_img = _split_palette(canvases, strip_w, GIF_STRIP_PALETTE_COLORS)
    frames = [c.quantize(palette=palette_img, dither=Image.FLOYDSTEINBERG) for c in canvases]
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations, loop=0,
    )
    out = buf.getvalue()
    if len(out) > GIF_SOFT_BYTES:
        # Мягкий лимит: файл технически влезает в Telegram, но 12МБ на 40 кадров
        # — это долгая загрузка превью и ноль запаса до жёсткого лимита.
        # Реальный кейс: видео-цитата весила 12.2МБ из 12.6МБ (97%).
        # None → вызывающий уменьшает кадры и/или холст по плану деградации.
        logger.debug(
            "quote gif: %.1fMB > soft %.1fMB — деградируем",
            len(out) / 1e6, GIF_SOFT_BYTES / 1e6)
        return None
    if len(out) > MAX_QUOTE_GIF_BYTES:
        return None
    return out


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
    max_width: int = 800,
    max_frames: int = 40,
    fps: int = 12,
    duration_s: float = 90.0,
    timeout_s: float = 90.0,
    anim_strips: list | None = None,
    anim_duration_ms: int = 0,
    circle: bool = False,
    target_width: int | None = None,
) -> bytes | None:
    """Видео/гифка/кружок из реплая справа + info-плашка слева → GIF.

    Если bytes уже GIF (реплай-гифка) — кадры берутся напрямую через Pillow
    без ffmpeg. Видео (mp4) и кружки (video_note, квадратный mp4) идут
    через ffmpeg; для кружков дополнительно ставится ``circle=True``, чтобы
    результат выглядел круглым, а не квадратным (круг рисует клиент).

    ``anim_strips``/``anim_duration_ms`` — фазы анимации custom-эмодзи в
    тексте (см. `_stack_gif_side_by_side`).

    ``duration_s`` — ТЕПЕРЬ только верхняя граница окна, а не точная длина:
    при ``fit_frames`` fps подбирается под реальную длительность клипа
    (``video_to_gif_bytes``), а лишние кадры равномерно прореживает
    `_even_frame_indices`. Так 60-секундный кружок показывается целиком,
    а не только первые 4 секунды.

    Returns None если нет кадров / encode упал / итог тяжелее лимита —
    caller падает на статичную PNG-цитату.
    """
    if not video_bytes or not card_png_bytes:
        return None
    gif = _as_gif_direct(video_bytes)
    if gif is None:
        try:
            from utils.gif_converter import video_to_gif_bytes
            gif = await video_to_gif_bytes(
                video_bytes,
                max_duration_s=duration_s,
                max_fps=fps,
                max_width=max_width,
                timeout_s=timeout_s,
                fit_frames=max_frames,
            )
        except Exception:
            return None
    if not gif:
        return None
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _stack_gif_side_by_side, gif, card_png_bytes, max_frames,
            max_width, anim_strips, anim_duration_ms, circle, target_width)
    except Exception:
        return None
