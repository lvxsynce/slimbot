"""Тесты .q / .цит: фон, GIF-сборка, caption, классификация media, триггеры."""

import asyncio
import io
from types import SimpleNamespace

from utils.quote_image import (
    _apply_background,
    _stack_gif_over_card,
    render_quote_png,
    render_video_quote_gif,
)
from handlers.commands.quote import (
    QUOTE_CMDS,
    _build_caption,
    _reply_media_kind,
)


def _png_bytes(color=(200, 30, 30), size=(800, 600)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def test_triggers_include_q():
    assert ".q" in QUOTE_CMDS
    assert ".цит" in QUOTE_CMDS
    assert ".quote" in QUOTE_CMDS


def test_build_caption_full():
    cap = _build_caption("Иван", "Петров", "Иван Петров", 123, ["ivan", "ip"], "2026-01-01 10:00")
    assert "<b>Иван Петров</b>" in cap
    assert "<code>123</code>" in cap
    assert "@ivan" in cap and "@ip" in cap
    assert "2026-01-01" in cap


def test_build_caption_minimal_and_escaped():
    cap = _build_caption("", "", "", None, [], "—")
    assert "?" in cap
    cap2 = _build_caption("<b>X</b>", "", "x", 1, ["<y>"], "")
    assert "<b>X</b>" not in cap2
    assert "&lt;b&gt;X&lt;/b&gt;" in cap2
    assert "@&lt;y&gt;" in cap2


def test_reply_media_kind():
    assert _reply_media_kind(None) is None
    assert _reply_media_kind(SimpleNamespace(
        document=None, video=None, video_note=None, animation=None,
        gif=None, photo=True, sticker=None)) == "photo"
    assert _reply_media_kind(SimpleNamespace(
        document=None, video=True, video_note=None, animation=None,
        gif=None, photo=None, sticker=None)) == "video"
    assert _reply_media_kind(SimpleNamespace(
        document=SimpleNamespace(mime_type="video/mp4"), video=None,
        video_note=None, animation=None, gif=None, photo=None,
        sticker=None)) == "video"
    assert _reply_media_kind(SimpleNamespace(
        document=SimpleNamespace(mime_type="image/jpeg"), video=None,
        video_note=None, animation=None, gif=None, photo=None,
        sticker=None)) == "photo"
    # стикеры — цитируются (в т.ч. анимированные TGS)
    assert _reply_media_kind(SimpleNamespace(
        document=None, video=None, video_note=None, animation=None,
        gif=None, photo=None, sticker=True)) == "sticker"
    assert _reply_media_kind(SimpleNamespace(
        document=SimpleNamespace(mime_type="application/x-tgsticker"),
        video=None, video_note=None, animation=None, gif=None,
        photo=None, sticker=None)) == "sticker"
    assert _reply_media_kind(SimpleNamespace(
        document=None, video=None, video_note=None, animation=None,
        gif=None, photo=None, sticker=None)) is None


def test_apply_background_keeps_canvas():
    from PIL import Image
    canvas = Image.new("RGB", (1200, 400), color=(32, 33, 38))
    out = _apply_background(canvas, _png_bytes())
    assert out.size == (1200, 400)
    assert out.mode == "RGB"
    # фон применён — пиксели отличаются от плоской заливки
    assert out.tobytes() != canvas.tobytes()


def test_render_with_background():
    out = render_quote_png(
        body="текст цитаты", sender_name="Иван Петров", sender_id=7,
        usernames=["ivan"], avatar_bytes=None, timestamp="t",
        background_bytes=_png_bytes(),
    )
    assert out is not None and len(out) > 5000
    from PIL import Image
    assert Image.open(io.BytesIO(out)).size[0] == 1400


def _two_frame_gif():
    from PIL import Image
    frames = [Image.new("RGB", (320, 200), color=c) for c in ((255, 0, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=150, loop=0)
    return buf.getvalue()


def _solid_gif(w: int, h: int, frames: int = 4, color=(40, 90, 160)):
    """Почти-одноцветная GIF заданного размера — вход для тестов геометрии.

    Кадры слегка различаются по яркости: Pillow схлопывает 100% одинаковые
    кадры в один (тот же баг, что обходится в `photo_to_gif_bytes`).
    """
    from PIL import Image, ImageEnhance
    imgs = []
    for i in range(frames):
        base = Image.new("RGB", (w, h), color=color)
        imgs.append(ImageEnhance.Brightness(base).enhance(1.0 + i * 0.02))
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:],
                 duration=100, loop=0)
    return buf.getvalue()


def test_stack_gif_over_card():
    card = render_quote_png(body="hi", sender_name="N", sender_id=1)
    assert card
    out = _stack_gif_over_card(_two_frame_gif(), card, max_frames=10)
    assert out is not None
    from PIL import Image, ImageSequence
    gif = Image.open(io.BytesIO(out))
    assert gif.size[0] == 320
    assert gif.size[1] > 200  # карточка добавлена снизу
    assert sum(1 for _ in ImageSequence.Iterator(gif)) == 2
    assert gif.info.get("loop") == 0


def test_stack_gif_empty_is_none():
    card = render_quote_png(body="hi", sender_name="N", sender_id=1)
    assert card
    buf = io.BytesIO()
    from PIL import Image
    Image.new("RGB", (10, 10)).save(buf, format="PNG")
    # PNG вместо GIF — кадров нет
    assert _stack_gif_over_card(buf.getvalue(), card) is None


def test_render_video_quote_gif_no_ffmpeg():
    # на этом хосте ffmpeg нет — deterministic None, без зависаний
    from utils import gif_converter
    if gif_converter.is_ffmpeg_available():
        return
    card = render_quote_png(body="hi", sender_name="N", sender_id=1)
    out = asyncio.run(render_video_quote_gif(b"fake-video", card_png_bytes=card, timeout_s=5))
    assert out is None


def test_quote_no_reply_branch():
    from handlers.commands.quote import handle

    edits = []

    class Event:
        chat_id = -100
        id = 1

        async def get_reply_message(self):
            return None

        async def edit(self, text, **kwargs):
            edits.append(text)

    asyncio.run(handle("u", Event()))
    assert edits and "Ответь этой командой" in edits[0]


def test_suggest_knows_q_aliases():
    from handlers.commands._helpdb import ALIASES
    assert ALIASES.get("q") == "quote"
    assert ALIASES.get("цит") == "quote"


def test_voice_body_and_duration():
    from handlers.commands.quote import _voice_body, _voice_duration

    assert _voice_body(SimpleNamespace(voice=None)) is None
    voice = SimpleNamespace(
        attributes=[SimpleNamespace(duration=75)], size=1234,
    )
    reply = SimpleNamespace(voice=voice)
    assert _voice_duration(reply) == 75
    assert _voice_body(reply) == "🎤 Голосовое сообщение (1:15)"
    reply2 = SimpleNamespace(voice=SimpleNamespace(attributes=[], size=10))
    assert _voice_duration(reply2) is None
    assert _voice_body(reply2) == "🎤 Голосовое сообщение"


def test_video_note_is_video_note_kind():
    # Кружок — отдельный вид: файл квадратный mp4, но нужна круглая маска.
    assert _reply_media_kind(SimpleNamespace(
        document=None, video=None, video_note=True, animation=None,
        gif=None, photo=None, sticker=None)) == "video_note"
    # Обычное видео остаётся "video" (без маски).
    assert _reply_media_kind(SimpleNamespace(
        document=None, video=True, video_note=None, animation=None,
        gif=None, photo=None, sticker=None)) == "video"


def _sine_m4a(duration_s: float = 1.0) -> bytes | None:
    """Синтезирует секундный тон в m4a через ffmpeg. None если нет ffmpeg."""
    import subprocess
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s}",
             "-c:a", "aac", "-movflags", "frag_keyframe+empty_moov",
             "-f", "mp4", "pipe:1"],
            capture_output=True, timeout=30,
        )
        return p.stdout or None
    except Exception:
        return None


def test_audio_info_music():
    from handlers.commands.quote import _audio_info
    AudioAttr = type("DocumentAttributeAudio", (), {})()
    AudioAttr.duration = 200
    AudioAttr.title = "Track"
    AudioAttr.performer = "Band"
    doc = SimpleNamespace(mime_type="audio/mpeg", attributes=[AudioAttr], size=1000)
    reply = SimpleNamespace(voice=None, audio=None, document=doc)
    is_audio, dur, label = _audio_info(reply)
    assert is_audio and dur == 200
    assert label == "🎵 Band — Track"
    assert _audio_info(SimpleNamespace(voice=None, audio=None, document=None)) == (False, None, "")


def test_image_audio_to_video_bytes():
    from utils.gif_converter import is_ffmpeg_available, image_audio_to_video_bytes
    if not is_ffmpeg_available():
        import pytest
        pytest.skip("no ffmpeg")
    tone = _sine_m4a()
    assert tone
    card = render_quote_png(body="hi", sender_name="N", sender_id=1)
    out = asyncio.run(image_audio_to_video_bytes(card, tone))
    assert out is not None and len(out) > 1000
    assert b"ftyp" in out[:32]


def test_quote_with_voice_attaches_voice():
    # Войс уходит ОДНИМ видео (карточка + звук внутри), без отдельного файла.
    from handlers.commands.quote import handle
    from utils.gif_converter import is_ffmpeg_available
    if not is_ffmpeg_available():
        import pytest
        pytest.skip("no ffmpeg")

    tone = _sine_m4a()
    assert tone
    sent_files = []
    deleted = []

    class VoiceDoc:
        size = 5000
        attributes = [SimpleNamespace(duration=5)]

    class Reply:
        raw_text = None
        message = None
        caption = None
        photo = None
        video = None
        video_note = None
        animation = None
        gif = None
        document = None
        sticker = None
        audio = None
        voice = VoiceDoc()
        date = None
        fwd_from = None
        entities = None

        async def get_sender(self):
            return SimpleNamespace(id=42, username="vova", first_name="Вова",
                                   last_name="Петров", deleted=False)

        async def download_media(self, file=None):
            return tone

    class Client:
        async def __call__(self, *args, **kwargs):
            raise RuntimeError("no mtproto")

        async def send_file(self, chat_id, file, **kwargs):
            sent_files.append((file, kwargs))
            return SimpleNamespace(id=777)

    class Event:
        chat_id = -100
        id = 1
        message = None
        client = Client()
        edits = []

        async def get_reply_message(self):
            return Reply()

        async def edit(self, text, **kwargs):
            self.edits.append(text)

        async def delete(self):
            deleted.append(True)

    asyncio.run(handle("u-voice", Event()))
    assert len(sent_files) == 1, [k for _, k in sent_files]
    video_file, video_kw = sent_files[0]
    assert getattr(video_file, "name", "") == "quote.mp4"
    attrs = video_kw.get("attributes") or []
    assert attrs and attrs[0].__class__.__name__ == "DocumentAttributeVideo"
    assert "caption" not in video_kw
    assert deleted == [True]


def test_quote_audio_with_caption_single_video():
    # Звук + текст: подпись на карточке, звук внутри видео. Один файл.
    from handlers.commands.quote import handle
    from utils.gif_converter import is_ffmpeg_available
    if not is_ffmpeg_available():
        import pytest
        pytest.skip("no ffmpeg")

    tone = _sine_m4a()
    assert tone
    sent_files = []

    AudioAttr = type("DocumentAttributeAudio", (), {})()
    AudioAttr.duration = 3
    AudioAttr.title = None
    AudioAttr.performer = None

    class Reply:
        raw_text = None
        message = None
        caption = "любимый трек"
        photo = None
        video = None
        video_note = None
        animation = None
        gif = None
        document = SimpleNamespace(mime_type="audio/mpeg", attributes=[AudioAttr], size=9999)
        sticker = None
        audio = True
        voice = None
        date = None
        fwd_from = None
        entities = None

        async def get_sender(self):
            return SimpleNamespace(id=7, username=None, first_name="A",
                                   last_name=None, deleted=False)

        async def download_media(self, file=None):
            return tone

    class Client:
        async def __call__(self, *args, **kwargs):
            raise RuntimeError("no mtproto")

        async def send_file(self, chat_id, file, **kwargs):
            sent_files.append((file, kwargs))
            return SimpleNamespace(id=888)

    class Event:
        chat_id = -100
        id = 5
        message = None
        client = Client()

        async def get_reply_message(self):
            return Reply()

        async def edit(self, text, **kwargs):
            pass

        async def delete(self):
            pass

    asyncio.run(handle("u-audio", Event()))
    assert len(sent_files) == 1
    assert getattr(sent_files[0][0], "name", "") == "quote.mp4"


def test_emoji_kept_and_rendered():
    from utils.quote_image import (
        _strip_html, _segment_runs, _emoji_glyph, _draw_runs,
        _resolve_font, BODY_FONT_SIZE,
    )
    from PIL import Image, ImageDraw

    assert "🎤" in _strip_html("hi 🎤 yo")
    runs = _segment_runs("a🎤b")
    assert ("t", "a") in runs and ("e", "🎤") in runs and ("t", "b") in runs
    g = _emoji_glyph("🎤", 32)
    assert g is not None and g.height > 0

    font = _resolve_font(BODY_FONT_SIZE, "hi")
    img = Image.new("RGB", (600, 80), color=(32, 33, 38))
    d = ImageDraw.Draw(img)
    end_x = _draw_runs(d, img, 10, 10, "hi 🎤", font, BODY_FONT_SIZE, (240, 240, 240))
    assert end_x > 60  # текст + глиф заняли место
    assert img.tobytes() != Image.new("RGB", (600, 80), color=(32, 33, 38)).tobytes()


def test_media_only_card_no_body():
    out = render_quote_png(body="", sender_name="Иван", sender_id=9,
                           usernames=[], avatar_bytes=None, timestamp="t")
    assert out is not None and len(out) > 1000
    from PIL import Image
    im = Image.open(io.BytesIO(out))
    assert im.size[0] == 1400
    assert im.size[1] == 600  # VERTICAL_SCALE: min 240×2.5, только header без bubble


def test_emoji_body_card():
    out = render_quote_png(body="привет 🎤 как дела ❤️", sender_name="N",
                           sender_id=1, background_bytes=_png_bytes((20, 120, 200)))
    assert out is not None and len(out) > 5000


def test_caption_capped():
    from handlers.commands.quote import _build_caption
    cap = _build_caption("A", "B", "AB", 1, [f"user{i}" for i in range(50)], "d")
    assert len(cap) <= 1000
    assert "…и ещё" in cap


def test_sticker_reply_becomes_photo_quote():
    from handlers.commands.quote import handle

    sent_files = []

    class Reply:
        raw_text = None
        message = None
        caption = None
        photo = None
        video = None
        video_note = None
        animation = None
        gif = None
        document = None
        sticker = True
        voice = None
        date = None
        fwd_from = None

        async def get_sender(self):
            return SimpleNamespace(id=77, username=None, first_name="Анна",
                                   last_name=None, deleted=False)

        async def download_media(self, file=None):
            raise RuntimeError("nothing to download")

    class Client:
        async def __call__(self, *args, **kwargs):
            raise RuntimeError("no mtproto")

        async def send_file(self, chat_id, file, **kwargs):
            sent_files.append((file, kwargs))
            return SimpleNamespace(id=555)

    class Event:
        chat_id = -100
        id = 2
        message = None
        client = Client()
        edits = []
        deleted = []

        async def get_reply_message(self):
            return Reply()

        async def edit(self, text, **kwargs):
            self.edits.append(text)

        async def delete(self):
            self.deleted.append(True)

    asyncio.run(handle("u-sticker", ev := Event()))
    assert len(sent_files) == 1
    assert not ev.edits or "Цитировать нечего" not in ev.edits[0]
    assert ev.deleted == [True]


def test_math_unicode_glyphs_covered():
    from utils.quote_image import _font_for, _font_has_glyph, BODY_FONT_SIZE
    f = _font_for("𝒍", BODY_FONT_SIZE, False)
    assert f is not None
    assert _font_has_glyph(f, "𝒍")
    assert _font_has_glyph(_font_for("A", BODY_FONT_SIZE, False), "A")
    assert _font_has_glyph(_font_for("Ж", BODY_FONT_SIZE, False), "Ж")


def test_math_name_card_renders():
    out = render_quote_png(body="обычный текст", sender_name="𝒍𝒆𝒗𝒆𝒍𝒔",
                           sender_id=123456789, usernames=["testuser"],
                           timestamp="2026-09-27 16:02")
    assert out is not None and len(out) > 5000


def test_quote_sent_without_caption():
    from handlers.commands.quote import handle

    sent_kwargs = []

    class Reply:
        raw_text = "привет"
        message = "привет"
        caption = None
        photo = None
        video = None
        video_note = None
        animation = None
        gif = None
        document = None
        sticker = None
        voice = None
        date = None
        fwd_from = None

        async def get_sender(self):
            return SimpleNamespace(id=1, username="u", first_name="N",
                                   last_name=None, deleted=False)

        async def download_media(self, file=None):
            raise RuntimeError("no media")

    class Client:
        async def __call__(self, *args, **kwargs):
            raise RuntimeError("no mtproto")

        async def send_file(self, chat_id, file, **kwargs):
            sent_kwargs.append(kwargs)
            return SimpleNamespace(id=777)

    class Event:
        chat_id = -100
        id = 3
        message = None
        client = Client()

        async def get_reply_message(self):
            return Reply()

        async def edit(self, text, **kwargs):
            pass

        async def delete(self):
            pass

    asyncio.run(handle("u-nocap", Event()))
    assert len(sent_kwargs) == 1
    assert "caption" not in sent_kwargs[0]


def test_two_column_photo_right_text_quoted():
    from utils.quote_image import LEFT_COL_W, COLUMN_GAP, PADDING_X
    out = render_quote_png(body="привет мир", sender_name="Иван",
                           sender_id=9, usernames=["ivan"], timestamp="t",
                           background_bytes=_png_bytes((20, 120, 200)))
    assert out is not None
    from PIL import Image
    im = Image.open(io.BytesIO(out)).convert("RGB")
    assert im.size[0] == 1400
    # Левая колонка темнее фона bubble: справа есть бокс (фото + текст).
    left_px = im.getpixel((PADDING_X + 10, 130))
    right_x0 = PADDING_X + LEFT_COL_W + COLUMN_GAP
    right_px = im.getpixel((right_x0 + 60, 130))
    assert left_px != right_px  # разные зоны: инфо слева, контент справа


def test_body_without_quote_marks():
    import utils.quote_image as qi
    seen = []
    orig = qi._draw_runs
    def spy(draw, img, x, y, text, font, emoji_px, fill, bold=False, inline=None):
        seen.append(text)
        return orig(draw, img, x, y, text, font, emoji_px, fill, bold=bold,
                    inline=inline)
    qi._draw_runs = spy
    try:
        render_quote_png(body="hello", sender_name="N", sender_id=1)
    finally:
        qi._draw_runs = orig
    assert seen
    assert not any("«" in s or "»" in s for s in seen)  # кавычек нет


def test_info_strip_renders():
    from utils.quote_image import render_info_strip_png, STRIP_WIDTH
    out = render_info_strip_png(sender_name="Иван", sender_id=9,
                                usernames=["ivan"], timestamp="t",
                                body="подпись к видео")
    assert out is not None and len(out) > 2000
    from PIL import Image
    im = Image.open(io.BytesIO(out))
    assert im.size[0] == STRIP_WIDTH
    assert im.size[1] >= 320


def test_gif_direct_path_no_ffmpeg_needed():
    # Реплай-гифка обязана стать анимированной цитатой БЕЗ ffmpeg —
    # это регрессия «пустой цитаты».
    from utils.quote_image import render_info_strip_png, _stack_gif_side_by_side
    strip = render_info_strip_png(sender_name="N", sender_id=1, body="hi")
    assert strip
    out = _stack_gif_side_by_side(_two_frame_gif(), strip, max_frames=10)
    assert out is not None
    from PIL import Image, ImageSequence
    gif = Image.open(io.BytesIO(out))
    assert sum(1 for _ in ImageSequence.Iterator(gif)) == 2
    assert gif.size[0] > 320  # плашка слева + кадры справа


def test_render_video_gif_from_reply_gif():
    # Сквозной: bytes настоящей гифки → анимированный результат.
    from utils.quote_image import render_info_strip_png
    strip = render_info_strip_png(sender_name="N", sender_id=1)
    out = asyncio.run(render_video_quote_gif(_two_frame_gif(), card_png_bytes=strip))
    assert out is not None
    from PIL import Image, ImageSequence
    assert sum(1 for _ in ImageSequence.Iterator(Image.open(io.BytesIO(out)))) == 2


def test_gif_color_fidelity():
    # Склейка не мнёт цвета: средние цвета правой части кадров ≈ исходным.
    from utils.quote_image import render_info_strip_png, _stack_gif_side_by_side
    strip = render_info_strip_png(sender_name="N", sender_id=1)
    out = _stack_gif_side_by_side(_two_frame_gif(), strip, max_frames=10)
    assert out is not None
    from PIL import Image, ImageSequence
    gif = Image.open(io.BytesIO(out))
    W, H = gif.size
    means = []
    for f in ImageSequence.Iterator(gif):
        rgb = f.convert("RGB")
        x0 = W - 640  # апскейл x2 исходных 320px
        crop = rgb.crop((x0 + 200, H // 2 - 20, x0 + 440, H // 2 + 20))
        px = list(crop.getdata())
        means.append(tuple(sum(c[i] for c in px) / len(px) for i in range(3)))
    assert len(means) == 2
    for got, want in zip(means, ((255, 0, 0), (0, 0, 255))):
        assert all(abs(g - w) < 12 for g, w in zip(got, want)), (got, want)


def test_gif_frames_upscaled():
    from utils.quote_image import render_info_strip_png, _stack_gif_side_by_side
    strip = render_info_strip_png(sender_name="N", sender_id=1)
    out = _stack_gif_side_by_side(_two_frame_gif(), strip, max_frames=10)
    from PIL import Image
    # исходник 320px → апскейл x2 = 640 + плашка слева
    assert Image.open(io.BytesIO(out)).size[0] > 640


def test_body_lines_compact():
    from utils.quote_image import BODY_LINE_HEIGHT
    from PIL import Image
    h1 = Image.open(io.BytesIO(render_quote_png(body="коротко", sender_name="N",
                                                sender_id=1))).size[1]
    h2 = Image.open(io.BytesIO(render_quote_png(body="слово " * 40, sender_name="N",
                                                sender_id=1))).size[1]
    assert h2 > h1  # перенос есть — текст на несколько строк
    lines2 = (h2 - h1) // 1
    # шаг высоты за строку текста ≈ BODY_LINE_HEIGHT, а не растянутые 120
    assert (h2 - h1) % BODY_LINE_HEIGHT == 0 or abs((h2 - h1) - BODY_LINE_HEIGHT) < BODY_LINE_HEIGHT







def test_extended_emoji_ranges_kept():
    # стрелки/фигуры больше не пропадают и не крашат wrap
    out = render_quote_png(body="a → b ▶ c #️⃣", sender_name="N", sender_id=1)
    assert out is not None and len(out) > 1000


def test_left_column_lines_do_not_overlap():
    # Регрессия: строки инфо рисовались на одной Y (без ly +=) друг на друге.
    from PIL import Image
    out = render_quote_png(body="hi", sender_name="Имя Фамилия", sender_id=42,
                           usernames=["u1", "u2"], timestamp="2026-01-01 10:00")
    assert out is not None
    im = Image.open(io.BytesIO(out)).convert("L")
    px = im.load()
    rows = [sum(1 for x in range(60, 520) if px[x, y] > 150) for y in range(im.height)]
    blocks = []
    inb = False
    for y, v in enumerate(rows):
        if v > 3 and not inb:
            inb, start = True, y
        elif v <= 3 and inb:
            inb = False
            blocks.append((start, y))
    # имя + ID + 2 username + дата = 5 раздельных строк
    assert len(blocks) >= 5, blocks


def _png_bytes(color=(200, 30, 30), size=(800, 600)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _synthetic_tgs() -> bytes:
    """Минимальный валидный TGS (gzip + Lottie JSON) с вращающимся квадратом."""
    import gzip
    import json
    lottie = {
        "v": "5.7.4", "fr": 60, "ip": 0, "op": 60, "w": 512, "h": 512,
        "nm": "t", "ddd": 0, "assets": [],
        "layers": [{
            "ddd": 0, "ind": 1, "ty": 4, "nm": "l", "sr": 1,
            "ks": {
                "o": {"a": 0, "k": 100},
                "r": {"a": 1, "k": [
                    {"i": {"x": [0.5], "y": [0.5]}, "o": {"x": [0.5], "y": [0.5]},
                     "t": 0, "s": [0]},
                    {"t": 60, "s": [360]}]},
                "p": {"a": 0, "k": [256, 256, 0]},
                "a": {"a": 0, "k": [0, 0, 0]},
                "s": {"a": 0, "k": [100, 100, 100]},
            },
            "ao": 0,
            "shapes": [{"ty": "gr", "nm": "g", "it": [
                {"ty": "rc", "d": 1, "nm": "r",
                 "s": {"a": 0, "k": [200, 200]}, "p": {"a": 0, "k": [0, 0]},
                 "r": {"a": 0, "k": 20}},
                {"ty": "fl", "nm": "f",
                 "c": {"a": 0, "k": [0.9, 0.2, 0.2, 1]},
                 "o": {"a": 0, "k": 100}, "r": 1},
                {"ty": "tr", "nm": "t",
                 "p": {"a": 0, "k": [0, 0]}, "a": {"a": 0, "k": [0, 0]},
                 "s": {"a": 0, "k": [100, 100]},
                 "r": {"a": 0, "k": 0}, "o": {"a": 0, "k": 100}},
            ]}],
            "ip": 0, "op": 60, "st": 0, "bm": 0,
        }],
    }
    return gzip.compress(json.dumps(lottie).encode())


def test_custom_emoji_spans_extraction():
    from telethon.tl.types import MessageEntityCustomEmoji, MessageEntityBold
    from handlers.commands.quote import _custom_emoji_spans
    ents = [MessageEntityBold(offset=0, length=2),
            MessageEntityCustomEmoji(offset=3, length=2, document_id=12345)]
    assert _custom_emoji_spans(SimpleNamespace(entities=ents)) == [(3, 2, 12345)]
    assert _custom_emoji_spans(SimpleNamespace(entities=None)) == []
    assert _custom_emoji_spans(SimpleNamespace()) == []


def test_emoji_placeholders_utf16():
    from handlers.commands.quote import _apply_emoji_placeholders
    # '🔥' — астральный символ = 2 UTF-16 юнита; entity offset/length в юнитах
    text, mapping = _apply_emoji_placeholders("hi 🔥 yo", [(3, 2, 999)])
    assert len(mapping) == 1
    pua, doc = next(iter(mapping.items()))
    assert doc == 999
    assert text == f"hi {pua} yo"
    # битый диапазон — текст цел
    text2, mapping2 = _apply_emoji_placeholders("hi", [(10, 2, 1)])
    assert text2 == "hi" and mapping2 == {}
    assert _apply_emoji_placeholders("", []) == ("", {})


def test_placeholders_only_when_available():
    from handlers.commands.quote import _apply_emoji_placeholders
    spans = [(3, 2, 111), (8, 2, 222)]
    text, mapping = _apply_emoji_placeholders("hi 🔥 yo 🎉!", spans, {111})
    assert list(mapping.values()) == [111]
    assert "🎉" in text  # не скачалось — исходный символ на месте
    assert "🔥" not in text


def test_inline_images_rendered():
    from PIL import Image
    red = Image.new("RGBA", (64, 64), color=(255, 0, 0, 255))
    out = render_quote_png(body="hi  bye", sender_name="N", sender_id=1,
                           inline_images={"": red})
    assert out is not None
    im = Image.open(io.BytesIO(out)).convert("RGB")
    # красный пиксель inline-картинки в правой колонке (бокс с x=560)
    found = False
    for x in range(560, im.width, 10):
        for y in range(0, im.height, 10):
            r, g, b = im.getpixel((x, y))
            if r > 200 and g < 80 and b < 80:
                found = True
                break
    assert found


def test_unknown_pua_skipped_no_crash():
    out = render_quote_png(body="hi  bye", sender_name="N", sender_id=1)
    assert out is not None and len(out) > 1000


def test_select_anim_phases():
    from handlers.commands.quote import _select_anim_phases
    assert _select_anim_phases(0) == []
    assert _select_anim_phases(1) == [0]
    assert _select_anim_phases(4) == [0, 1, 2, 3]
    ph = _select_anim_phases(36)
    assert len(ph) == 8 and ph[0] == 0 and ph[-1] == 35


def test_stack_anim_strips_change_over_time():
    from utils.quote_image import (
        render_info_strip_png, _stack_gif_side_by_side)
    from PIL import Image, ImageSequence
    s1 = render_info_strip_png(sender_name="N", sender_id=1, body="phase one")
    s2 = render_info_strip_png(sender_name="N", sender_id=1,
                               body="phase two is longer here")
    assert s1 != s2
    out = _stack_gif_side_by_side(_two_frame_gif(), s1, max_frames=10,
                                  anim_strips=[s1, s2], anim_duration_ms=300)
    assert out is not None
    frames = [f.convert("RGB") for f in ImageSequence.Iterator(
        Image.open(io.BytesIO(out)))]
    assert len(frames) == 2
    import hashlib
    h = [hashlib.md5(f.crop((0, 0, 100, f.height)).tobytes()).hexdigest()
         for f in frames]
    assert h[0] != h[1], "фазы эмодзи не сменились между кадрами"


def test_inline_affects_width():
    from utils.quote_image import _resolve_font, _wrap_text, BODY_FONT_SIZE
    from PIL import Image
    font = _resolve_font(BODY_FONT_SIZE, "hi")
    red = Image.new("RGBA", (100, 50), color=(255, 0, 0, 255))
    w_inline = _wrap_text("ab", font, max_width=5000, emoji_px=BODY_FONT_SIZE,
                          inline={"": red})
    assert w_inline == ["ab"]  # маркер без картинки не ломает перенос


def test_emoji_never_vanishes_when_fetch_fails():
    """Если кадры не скачались — эмодзи остаётся исходным символом.

    Регрессия: `available=None` в _apply_emoji_placeholders означает «заменить
    все», из-за чего непокрытые эмодзи превращались в PUA-маркеры без
    картинки и молча исчезали из цитаты.
    """
    import asyncio
    import handlers.commands.quote as q
    from telethon.tl.types import MessageEntityCustomEmoji
    text = "hi \U0001F525 yo"
    spans = q._custom_emoji_spans(SimpleNamespace(
        entities=[MessageEntityCustomEmoji(offset=3, length=2, document_id=1)]))
    # available=set() — ничего не скачалось
    out, mapping = q._apply_emoji_placeholders(text, spans, set())
    assert mapping == {}
    assert "\U0001F525" in out, "эмодзи исчез из текста"

    class Client:
        async def __call__(self, req):
            raise RuntimeError("network down")

    res = asyncio.run(q._fetch_custom_emoji(Client(), [1]))
    assert res == {}
    # handle() обязан звать с set() (пустым), а не с None
    out2, mapping2 = q._apply_emoji_placeholders(text, spans, set(res))
    assert mapping2 == {} and "\U0001F525" in out2


def test_render_pua_without_image_drops_marker_cleanly():
    """PUA-маркер без картинки не рисуется (и не превращается в tofu).

    Сравниваем три рендера: маркер без картинки должен дать ровно столько
    же «чернил», сколько обычный пробел, а с картинкой — заметно больше.
    """
    from PIL import Image, ImageDraw
    from utils.quote_image import _resolve_font, _draw_runs, BODY_FONT_SIZE
    font = _resolve_font(BODY_FONT_SIZE, "x")
    marker = ""

    def render(text, inline=None):
        img = Image.new("RGB", (400, 80), (0, 0, 0))
        _draw_runs(ImageDraw.Draw(img), img, 10, 10, text, font,
                   BODY_FONT_SIZE, (255, 255, 255), inline=inline)
        return sum(1 for px in img.getdata() if sum(px) > 200)

    no_ink = render(f"a{marker}b")        # маркер без картинки → ноль чернил
    space = render("a b")                 # контроль: тоже ноль чернил
    assert no_ink == space, (
        f"PUA-маркер отрисовался (tofu): {no_ink} != {space}")

    from PIL import Image as _I
    with_img = render(f"a{marker}b", inline={marker: _I.new("RGBA", (32, 32), (255, 0, 0, 255))})
    assert with_img > space, "inline-картинка не отрисовалась"


def test_tgs_to_rgba_frames_real_lottie():
    """TGS → RGBA-кадры через rlottie. Skipped если пакет не установлен."""
    from utils.gif_converter import tgs_to_rgba_frames
    frames = tgs_to_rgba_frames(_synthetic_tgs())
    if not frames:
        import pytest
        pytest.skip("rlottie-python не установлен")
    assert len(frames) > 1, "ожидалась анимация из нескольких кадров"
    from PIL import Image
    for f in frames:
        assert f.mode == "RGBA"
        assert f.width == f.height, "эмодзи должен быть квадратным"
    # Настоящая прозрачность: у квадрата на прозрачном фоне есть alpha=0.
    alphas = [p[3] for p in list(frames[0].getdata())[::17]]
    assert min(alphas) == 0, "alpha не пробрасывается"
    assert max(alphas) == 255


def test_tgs_renders_without_touching_network():
    """Рендер TGS не должен требовать сети/файловой системы вне tmp."""
    from utils.gif_converter import tgs_to_rgba_frames
    frames = tgs_to_rgba_frames(_synthetic_tgs(), max_frames=4, size=64)
    if not frames:
        import pytest
        pytest.skip("rlottie-python не установлен")
    assert len(frames) == 4
    assert all(f.size == (64, 64) for f in frames)


def test_tgs_non_gzip_and_garbage_safe():
    """Мусор на входе → [], без исключений."""
    from utils.gif_converter import tgs_to_rgba_frames
    assert tgs_to_rgba_frames(b"") == []
    assert tgs_to_rgba_frames(b"not a tgs at all") == []
    assert tgs_to_rgba_frames(b"\x1f\x8b" + b"\x00" * 40) == []


def test_fetch_custom_emoji_tgs_uses_rlottie():
    """TGS-эмодзи идёт через rlottie, а не через статичный thumb."""
    import asyncio
    from handlers.commands.quote import _fetch_custom_emoji
    from utils.gif_converter import tgs_to_rgba_frames
    if not tgs_to_rgba_frames(_synthetic_tgs()):
        import pytest
        pytest.skip("rlottie-python не установлен")
    tgs = _synthetic_tgs()

    class Client:
        def __init__(self):
            self.thumb_calls = 0

        async def __call__(self, req):
            assert req.__class__.__name__ == "GetCustomEmojiDocumentsRequest"
            return [SimpleNamespace(id=777, mime_type="application/x-tgsticker",
                                    thumbs=[])]

        async def download_media(self, doc, file=None, thumb=None):
            if thumb is not None:
                self.thumb_calls += 1
            return tgs

    c = Client()
    res = asyncio.run(_fetch_custom_emoji(c, [777]))
    assert 777 in res
    assert len(res[777]["frames"]) > 1, "ожидалась анимация"
    assert c.thumb_calls == 0, "thumb не нужен, когда rlottie отработал"


def test_fetch_custom_emoji_tgs_falls_back_to_photo_thumb():
    """Без rlottie берём ФОТО-thumb, перебирая thumbs по индексу.

    `thumb=-1` («наибольший») у анимированных стикеров может указывать на
    VideoSize (webm), который Pillow не открывает — поэтому индексы.
    """
    import asyncio
    import handlers.commands.quote as q
    import utils.gif_converter as g
    webp = _png_bytes((10, 200, 90), size=(100, 100))
    webm = b"\x1a\x45\xdf\xa3" + b"\x00" * 64   # webm — Pillow не откроет

    class Client:
        def __init__(self):
            self.thumbs_tried = []

        async def __call__(self, req):
            return [SimpleNamespace(
                id=777, mime_type="application/x-tgsticker",
                thumbs=["v", "p"],  # два thumb'а
            )]

        async def download_media(self, doc, file=None, thumb=None):
            if thumb is None:
                return b"\x1f\x8b" + b"\x00" * 40  # битый gzip → rlottie сдаст
            self.thumbs_tried.append(thumb)
            return webm if thumb == 0 else webp

    orig_tgs = g.tgs_to_rgba_frames
    g.tgs_to_rgba_frames = lambda *a, **k: []   # эмулируем «rlottie нет»
    try:
        c = Client()
        res = asyncio.run(q._fetch_custom_emoji(c, [777]))
        assert 777 in res
        assert len(res[777]["frames"]) == 1
        # index 0 (webm) не декодировался → пошли к index 1 (фото)
        assert c.thumbs_tried == [0, 1], c.thumbs_tried
    finally:
        g.tgs_to_rgba_frames = orig_tgs


def test_fetch_custom_emoji_tgs_no_thumbs_is_safe():
    """У TGS без thumbs не падаем — эмодзи просто останется alt-глифом."""
    import asyncio
    import handlers.commands.quote as q
    import utils.gif_converter as g

    class Client:
        async def __call__(self, req):
            return [SimpleNamespace(id=777, mime_type="application/x-tgsticker",
                                    thumbs=None)]

        async def download_media(self, doc, file=None, thumb=None):
            return b"\x1f\x8b" + b"\x00" * 40

    orig_tgs = g.tgs_to_rgba_frames
    g.tgs_to_rgba_frames = lambda *a, **k: []
    try:
        res = asyncio.run(q._fetch_custom_emoji(Client(), [777]))
        assert res == {}, "без кадров документ не должен попасть в res"
    finally:
        g.tgs_to_rgba_frames = orig_tgs


def test_fetch_custom_emoji_webm_frames():
    import asyncio
    import subprocess
    from handlers.commands.quote import _fetch_custom_emoji
    from utils.gif_converter import is_ffmpeg_available
    if not is_ffmpeg_available():
        import pytest
        pytest.skip("no ffmpeg")
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=0.5:size=100x100:rate=10",
         "-c:v", "libvpx-vp9", "-f", "webm", "pipe:1"],
        capture_output=True, timeout=60)
    assert p.stdout, "synth webm failed"

    class Client:
        async def __call__(self, req):
            return [SimpleNamespace(id=888, mime_type="video/webm")]

        async def download_media(self, doc, file=None, thumb=None):
            assert thumb is None
            return p.stdout

    res = asyncio.run(_fetch_custom_emoji(Client(), [888]))
    assert 888 in res
    assert len(res[888]["frames"]) > 1
    assert all(f.mode == "RGBA" for f in res[888]["frames"])


def test_fetch_custom_emoji_partial_and_duplicates():
    """Часть эмодзи недоступна → лишние не мешают, дубли схлопываются."""
    import asyncio
    from handlers.commands.quote import _fetch_custom_emoji

    class Client:
        def __init__(self):
            self.asked = None

        async def __call__(self, req):
            self.asked = list(req.document_id)
            return [SimpleNamespace(id=1, mime_type="image/webp")]

        async def download_media(self, doc, file=None, thumb=None):
            from PIL import Image
            b = io.BytesIO()
            Image.new("RGBA", (32, 32), (1, 2, 3, 255)).save(b, format="PNG")
            return b.getvalue()

    c = Client()
    res = asyncio.run(_fetch_custom_emoji(c, [1, 1, 2]))
    assert c.asked == [1, 2], "дубли document_id должны схлопываться"
    assert set(res) == {1}, "document_id=2 не вернулся — в res не должен попасть"


def _circle_note_mp4() -> bytes:
    """Квадратный mp4 200x200 (как отдаёт Telegram для кружка) через ffmpeg.

    ``-movflags frag_keyframe+empty_moov`` обязателен: mp4-муксер требует
    seekable output, а pipe — не seekable.
    """
    import subprocess
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=200x200:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"],
        capture_output=True, timeout=60)
    return p.stdout


def test_video_note_becomes_animated_gif():
    """Кружок → анимированная GIF-цитата (а не статичная заглушка)."""
    from utils.gif_converter import is_ffmpeg_available
    if not is_ffmpeg_available():
        import pytest
        pytest.skip("no ffmpeg")
    from handlers.commands.quote import handle
    mp4 = _circle_note_mp4()
    assert mp4, "synth mp4 failed"

    sent = []
    deleted = []

    class Reply:
        raw_text = None
        message = None
        caption = None
        photo = None
        video = None
        video_note = SimpleNamespace(size=len(mp4))
        animation = None
        gif = None
        document = SimpleNamespace(mime_type="video/mp4", size=len(mp4))
        sticker = None
        audio = None
        voice = None
        date = None
        fwd_from = None
        entities = None

        async def get_sender(self):
            return SimpleNamespace(id=5, username=None, first_name="К",
                                   last_name=None, deleted=False)

        async def download_media(self, file=None, **kw):
            return mp4

    class Client:
        async def __call__(self, *args, **kwargs):
            raise RuntimeError("no mtproto")

        async def send_file(self, chat_id, file, **kwargs):
            sent.append((getattr(file, "name", ""), file.getvalue()
                         if hasattr(file, "getvalue") else None, kwargs))
            return SimpleNamespace(id=111)

    class Event:
        chat_id = -100
        id = 9
        message = None
        client = Client()

        async def get_reply_message(self):
            return Reply()

        async def edit(self, text, **kwargs):
            pass

        async def delete(self):
            deleted.append(True)

    asyncio.run(handle("u-note", Event()))
    assert len(sent) == 1
    name, raw, kwargs = sent[0]
    assert name == "quote.gif", f"ожидалась GIF-цитата, получено {name}"
    assert kwargs.get("mime_type") == "image/gif"
    # Атрибут animated → Telegram рендерит как inline-playable превью.
    assert any(a.__class__.__name__ == "DocumentAttributeAnimated"
               for a in kwargs.get("attributes", []))
    assert deleted == [True]

    from PIL import Image, ImageSequence
    gif = Image.open(io.BytesIO(raw))
    frames = list(ImageSequence.Iterator(gif))
    assert len(frames) >= 2, "кружок должен стать анимацией"


def test_video_note_gif_is_circular():
    """Круглая маска: углы кадра прозрачные/фоновые, центр — видео."""
    from utils.gif_converter import is_ffmpeg_available
    if not is_ffmpeg_available():
        import pytest
        pytest.skip("no ffmpeg")
    from utils.quote_image import render_info_strip_png, _stack_gif_side_by_side
    from utils.gif_converter import video_to_gif_bytes

    mp4 = _circle_note_mp4()
    gif = asyncio.run(video_to_gif_bytes(
        mp4, max_duration_s=2.0, max_fps=8, max_width=200, timeout_s=60))
    assert gif, "video->gif не отработал"
    strip = render_info_strip_png(sender_name="N", sender_id=1, body="hi")
    out = _stack_gif_side_by_side(gif, strip, max_frames=6, circle=True)
    assert out is not None
    from PIL import Image, ImageSequence
    fr = next(iter(ImageSequence.Iterator(Image.open(io.BytesIO(out)))))
    rgb = fr.convert("RGB")
    w, h = rgb.size
    # Геометрия та же, что в _stack_gif_side_by_side: плашка масштабируется
    # под высоту кадра, видео идёт правее на свою ширину.
    strip_h = Image.open(io.BytesIO(strip)).height if isinstance(strip, bytes) else strip.height
    strip_w_pt = Image.open(io.BytesIO(strip)).width if isinstance(strip, bytes) else strip.width
    strip_w = max(1, int(strip_w_pt * h / max(strip_h, 1)))
    video_w = w - strip_w
    assert video_w > 20, f"видео-область не найдена: w={w} strip_w={strip_w}"
    # Углы видео-области после маски = тёмный фон, центр = кадр testsrc.
    corner = rgb.getpixel((strip_w + 2, 2))
    center = rgb.getpixel((strip_w + video_w // 2, h // 2))
    assert sum(corner) < sum(center), (
        f"круглая маска не наложена: угол={corner} центр={center}")


def test_apply_circle_mask_rounds_corners():
    from utils.gif_converter import apply_circle_mask
    from PIL import Image
    src = Image.new("RGB", (200, 200), (255, 0, 0))
    m = apply_circle_mask(src)
    assert m.mode == "RGBA"
    c = 100
    assert m.getpixel((c, c))[3] == 255             # центр непрозрачен
    assert m.getpixel((0, 0))[3] < 20               # угол вырезан
    assert m.getpixel((c, 0))[3] < 20               # верх у края вырезан (margin)
    # Чуть ниже верхнего края круг уже есть (margin ≈ 4% от 200 = 8px).
    assert m.getpixel((c, 20))[3] > 200


def test_apply_circle_mask_margin_zero_touches_edge():
    """margin_ratio=0 — круг вплотную к краям (обратная совместимость)."""
    from utils.gif_converter import apply_circle_mask
    from PIL import Image
    src = Image.new("RGB", (200, 200), (255, 0, 0))
    m = apply_circle_mask(src, margin_ratio=0.0)
    assert m.getpixel((100, 0))[3] > 200, "при margin=0 верх должен быть непрозрачным"
    assert m.getpixel((0, 0))[3] < 20


def test_gif_quote_fills_target_width():
    """GIF-цитата по ширине догоняет PNG-карточку (регрессия «мелкой гифки»)."""
    from utils.quote_image import (
        render_info_strip_png, _stack_gif_side_by_side, WIDTH)
    strip = render_info_strip_png(sender_name="N", sender_id=1, body="hi")
    # Кадр 200×200 (размер кружка в Telegram) — раньше жёсткий лимит «не более
    # 2x» давал холст ~926px вместо ~1400px, и круг занимал треть цитаты.
    gif = _solid_gif(200, 200, frames=6)
    out = _stack_gif_side_by_side(gif, strip, 6, 800, None, 0, True)
    assert out is not None
    from PIL import Image
    w, h = Image.open(io.BytesIO(out)).size
    # Ширина: близка к WIDTH, круг заметно больше прежних 400px.
    assert w >= WIDTH * 0.7, f"холст слишком узкий: {w} (ожидалось ~{WIDTH})"
    assert h >= 400, f"круг не растянут: высота {h}"


def test_gif_quote_keeps_strip_text_colors():
    """Насыщенное видео не перекрашивает текст info-плашки.

    Регрессия: общий MEDIANCUT по всему монтажу отдавал палитру кадру, и
    серо-синий NAME_COLOR маппился на оранжевый.
    """
    from utils.quote_image import (
        render_info_strip_png, _stack_gif_side_by_side, NAME_COLOR)
    strip = render_info_strip_png(sender_name="Иван", sender_id=1, body="привет")
    # Кадр полностью оранжевый — враждебный для синего текста.
    gif = _solid_gif(200, 200, frames=4, color=(255, 120, 0))
    out = _stack_gif_side_by_side(gif, strip, 4, 800, None, 0, False)
    assert out is not None
    from PIL import Image, ImageSequence
    fr = next(iter(ImageSequence.Iterator(Image.open(io.BytesIO(out)))))
    rgb = fr.convert("RGB")
    # Ищем пиксели, близкие к NAME_COLOR (сине-серый) — они должны сохраниться.
    target = NAME_COLOR
    found = 0
    for x in range(0, min(400, rgb.width), 2):
        for y in range(0, rgb.height, 2):
            r, g, b = rgb.getpixel((x, y))
            if (abs(r - target[0]) < 28 and abs(g - target[1]) < 28
                    and abs(b - target[2]) < 28):
                found += 1
    assert found > 20, (
        f"цвет текста плашки не сохранился (близких пикселей: {found}) — "
        f"палитра съедена кадром")


def _moov_at_end_mp4(duration_s: int = 40) -> bytes:
    """mp4 БЕЗ faststart (moov в конце) — ровно как хранит Telegram.

    Регрессия: `video_to_gif_bytes` раньше кормила ffmpeg через `pipe:0`.
    На не-seekable stdin mp4 с moov в конце не читается —
    «stream 0, offset 0x30: partial file», rc=183, пустой stdout.

    Пишем через temp-файл: mp4-муксер отказывается писать НЕ-фрагментированный
    mp4 в pipe («muxer does not support non seekable output»), поэтому
    «честный» moov-в-конце контейнер можно получить только с файла.
    """
    import os
    import subprocess
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i",
             f"testsrc=duration={duration_s}:size=200x200:rate=30",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
            capture_output=True, timeout=300)
        assert p.returncode == 0, p.stderr[-300:]
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_video_to_gif_reads_non_faststart_mp4():
    """mp4 с moov в конце конвертится (регрессия на pipe:0)."""
    from utils.gif_converter import video_to_gif_bytes, is_ffmpeg_available
    if not is_ffmpeg_available():
        import pytest
        pytest.skip("no ffmpeg")
    data = _moov_at_end_mp4()
    # Sanity: moov действительно в хвосте (иначе тест не проверяет баг).
    assert data.rfind(b"moov") > len(data) * 0.5, "moov не в конце — тест невалиден"
    assert len(data) > 100_000, "файл слишком мал — баг мог не проявиться"
    out = asyncio.run(video_to_gif_bytes(
        data, max_duration_s=3.0, max_fps=10, max_width=400, timeout_s=120))
    assert out, "video_to_gif_bytes вернул None на mp4 с moov в конце"
    from PIL import Image, ImageSequence
    assert len(list(ImageSequence.Iterator(Image.open(io.BytesIO(out))))) >= 2


def test_video_to_gif_works_for_webm_too():
    from utils.gif_converter import video_to_gif_bytes, is_ffmpeg_available
    if not is_ffmpeg_available():
        import pytest
        pytest.skip("no ffmpeg")
    import subprocess
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=160x160:rate=10",
         "-c:v", "libvpx-vp9", "-f", "webm", "pipe:1"],
        capture_output=True, timeout=60)
    assert p.stdout
    out = asyncio.run(video_to_gif_bytes(
        p.stdout, max_duration_s=2.0, max_fps=8, max_width=320, timeout_s=90))
    assert out, "webm не сконвертился"


def test_video_ext_detection():
    from utils.gif_converter import _video_ext
    assert _video_ext(b"\x1a\x45\xdf\xa3" + b"\x00" * 20) == ".webm"
    assert _video_ext(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20) == ".mp4"
    assert _video_ext(b"RIFF\x00\x00\x00\x00AVI ") == ".avi"
    # Неизвестный магик → mp4 (частый случай Telegram), ffmpeg проверит сам.
    assert _video_ext(b"garbage-magic-bytes") == ".mp4"


def test_sample_plan_thins_evenly_and_reports_stride():
    """Равномерная выборка + шаг для сохранения реального темпа."""
    import pytest
    from utils.quote_image import _sample_plan
    # Кадров меньше лимита — не прореживаем.
    idx, stride = _sample_plan(10, 40)
    assert idx is None and stride == 1.0
    # Кадров больше лимита — равномерно по всей длительности.
    idx, stride = _sample_plan(360, 40)
    assert idx is not None
    assert len(idx) == 40
    assert min(idx) == 0 and max(idx) == 359, "крайние кадры (начало и КОНЕЦ)"
    assert max(idx) > 300, "конец ролика должен попасть в выборку"
    assert stride == pytest.approx(360 / 40, rel=1e-6)
    # Неизвестное число кадров — «бери всё».
    idx, stride = _sample_plan(0, 40)
    assert idx is None and stride == 1.0


def test_quote_gif_covers_whole_video_not_just_start():
    """Регрессия: бралось только начало ролика (жёсткий -t 4.0).

    Берём видео «красная половина → зелёная половина»: если цитата показывает
    только начало, зелёного в ней не будет.
    """
    from utils.gif_converter import video_to_gif_bytes, is_ffmpeg_available
    if not is_ffmpeg_available():
        import pytest
        pytest.skip("no ffmpeg")
    import subprocess
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=200x200:d=10:r=30",
         "-f", "lavfi", "-i", "color=c=green:s=200x200:d=10:r=30",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"],
        capture_output=True, timeout=300)
    assert p.stdout, "synth mp4 failed"
    from utils.quote_image import render_info_strip_png, _stack_gif_side_by_side
    strip = render_info_strip_png(sender_name="N", sender_id=1, body="x", scale=2.0)
    # duration_s=90 — цитата обязана забрать весь клип, а не первые 4 с.
    gif = asyncio.run(video_to_gif_bytes(
        p.stdout, max_duration_s=90.0, max_fps=12, max_width=400,
        timeout_s=120, fit_frames=40))
    assert gif, "ffmpeg не сконвертировал"
    from PIL import Image, ImageSequence
    # Окно = вся длительность (20с), fps подогнан под fit_frames=40 с полом 3fps
    # → 60 кадров на весь ролик. Проверка «красная/зелёная половина» ниже —
    # главное доказательство; счётчик кадров — лишь sanity bound.
    assert Image.open(io.BytesIO(gif)).n_frames >= 40, (
        "конвертация должна покрыть весь клип, а не первые секунды")
    out = _stack_gif_side_by_side(gif, strip, 40, 400, None, 0, False)
    assert out is not None
    im = Image.open(io.BytesIO(out))

    def dominant(frame):
        px = list(frame.convert("RGB").crop(
            (im.width - 100, 0, im.width, im.height)).getdata())
        n = len(px)
        r = sum(q[0] for q in px) / n
        g = sum(q[1] for q in px) / n
        return "R" if r > g + 40 else ("G" if g > r + 40 else "?")

    seq = [dominant(f) for f in ImageSequence.Iterator(im)]
    assert "R" in seq, "нет кадров из начала ролика"
    assert "G" in seq, (
        f"в цитате нет кадров из КОНЦА ролика (только начало) — seq={seq}")


def test_frames_to_gif_bytes_roundtrip():
    from utils.gif_converter import frames_to_gif_bytes
    from PIL import Image
    frames = [Image.new("RGBA", (32, 32), (i * 60, 10, 20, 255)) for i in range(4)]
    out = frames_to_gif_bytes(frames, [80] * 4)
    assert out, "frames_to_gif_bytes вернул None"
    from PIL import ImageSequence
    gif = Image.open(io.BytesIO(out))
    assert len(list(ImageSequence.Iterator(gif))) >= 2
    # Один кадр — не анимация.
    assert frames_to_gif_bytes(frames[:1]) is None
    assert frames_to_gif_bytes([]) is None


def test_sticker_tgs_becomes_animated_quote_gif():
    """Реплой на TGS-стикер → анимированная GIF-цитата (было: пустая карточка)."""
    import handlers.commands.quote as q
    from utils.gif_converter import tgs_to_rgba_frames
    if not tgs_to_rgba_frames(_synthetic_tgs()):
        import pytest
        pytest.skip("rlottie-python не установлен")
    tgs = _synthetic_tgs()

    class Reply:
        raw_text = None
        message = None
        caption = None
        photo = None
        video = None
        video_note = None
        animation = None
        gif = None
        document = SimpleNamespace(mime_type="application/x-tgsticker",
                                   size=len(tgs))
        sticker = SimpleNamespace(id=42, size=len(tgs))
        audio = None
        voice = None
        date = None
        fwd_from = None
        entities = None

        async def get_sender(self):
            return SimpleNamespace(id=5, username=None, first_name="К",
                                   last_name=None, deleted=False)

        async def download_media(self, file=None, **kw):
            return tgs

    sent = []
    deleted = []

    class Client:
        async def __call__(self, *a, **k):
            raise RuntimeError("no mtproto")

        async def send_file(self, chat_id, file, **kw):
            sent.append((getattr(file, "name", ""),
                         file.getvalue() if hasattr(file, "getvalue") else None,
                         kw))
            return SimpleNamespace(id=111)

    class Event:
        chat_id = -100
        id = 9
        message = None
        client = Client()

        async def get_reply_message(self):
            return Reply()

        async def edit(self, text, **kwargs):
            pass

        async def delete(self):
            deleted.append(True)

    asyncio.run(q.handle("u-sticker", Event()))
    assert len(sent) == 1, f"ожидался 1 файл, получено {len(sent)}"
    name, raw, kw = sent[0]
    assert name == "quote.gif", f"ожидалась GIF-цитата, получено {name}"
    assert kw.get("mime_type") == "image/gif"
    from PIL import Image, ImageSequence
    assert len(list(ImageSequence.Iterator(Image.open(io.BytesIO(raw))))) >= 2


def test_sticker_static_png_uses_photo_path():
    """Статичный webp/png-стикер → фон карточки (не GIF)."""
    import handlers.commands.quote as q
    png = _png_bytes((200, 40, 40), size=(256, 256))

    class Reply:
        raw_text = None
        message = None
        caption = None
        photo = None
        video = None
        video_note = None
        animation = None
        gif = None
        document = SimpleNamespace(mime_type="image/webp", size=len(png))
        sticker = SimpleNamespace(id=43, size=len(png))
        audio = None
        voice = None
        date = None
        fwd_from = None
        entities = None

        async def get_sender(self):
            return SimpleNamespace(id=5, username=None, first_name="К",
                                   last_name=None, deleted=False)

        async def download_media(self, file=None, **kw):
            return png

    seen = {}
    orig = q._render_card_png

    async def spy(*a, **kw):
        bg = a[6] if len(a) > 6 else kw.get("background_bytes")
        seen["bg"] = len(bg or b"")
        return await orig(*a, **kw)

    class Client:
        async def __call__(self, *a, **k):
            raise RuntimeError("no mtproto")

        async def send_file(self, chat_id, file, **kw):
            return SimpleNamespace(id=111)

    class Event:
        chat_id = -100
        id = 9
        message = None
        client = Client()

        async def get_reply_message(self):
            return Reply()

        async def edit(self, text, **kwargs):
            pass

        async def delete(self):
            pass

    q._render_card_png = spy
    try:
        asyncio.run(q.handle("u-static", Event()))
    finally:
        q._render_card_png = orig
    assert seen.get("bg") == len(png), (
        f"статичный стикер должен попасть в background_bytes, got {seen}")


def test_split_png_stream_recovers_frames():
    from utils.gif_converter import _split_png_stream
    from PIL import Image
    parts = []
    for i in range(3):
        b = io.BytesIO()
        Image.new("RGBA", (8, 8), (i * 40, 0, 0, 255)).save(b, format="PNG")
        parts.append(b.getvalue())
    out = _split_png_stream(b"".join(parts))
    assert len(out) == 3
    assert all(Image.open(io.BytesIO(x)).size == (8, 8) for x in out)
    assert _split_png_stream(b"") == []


def test_webm_frame_durations_uniform():
    from utils.gif_converter import webm_frame_durations
    assert webm_frame_durations(0) == []
    d = webm_frame_durations(6, fps=24)
    assert len(d) == 6 and all(x == int(1000 / 24) for x in d)
