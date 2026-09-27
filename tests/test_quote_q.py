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
    # стикеры и TGS — не цитируемое media
    assert _reply_media_kind(SimpleNamespace(
        document=None, video=None, video_note=None, animation=None,
        gif=None, photo=None, sticker=True)) is None
    assert _reply_media_kind(SimpleNamespace(
        document=SimpleNamespace(mime_type="application/x-tgsticker"),
        video=None, video_note=None, animation=None, gif=None,
        photo=None, sticker=None)) is None
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


def test_video_note_is_video_kind():
    assert _reply_media_kind(SimpleNamespace(
        document=None, video=None, video_note=True, animation=None,
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
                           sender_id=8002855167, usernames=["lv0xn"],
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
        return orig(draw, img, x, y, text, font, emoji_px, fill, bold=bold, inline=inline)
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


def test_custom_emoji_spans_extraction():
    from telethon.tl.types import MessageEntityCustomEmoji, MessageEntityBold
    from handlers.commands.quote import _custom_emoji_spans
    ents = [MessageEntityBold(offset=0, length=2),
            MessageEntityCustomEmoji(offset=3, length=2, document_id=12345)]
    spans = _custom_emoji_spans(SimpleNamespace(entities=ents))
    assert spans == [(3, 2, 12345)]
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


def test_inline_affects_width():
    from utils.quote_image import _resolve_font, _wrap_text, BODY_FONT_SIZE
    font = _resolve_font(BODY_FONT_SIZE, "hi")
    from PIL import Image
    red = Image.new("RGBA", (100, 50), color=(255, 0, 0, 255))
    w_plain = _wrap_text("ab", font, max_width=5000, emoji_px=BODY_FONT_SIZE)
    w_inline = _wrap_text("ab", font, max_width=5000, emoji_px=BODY_FONT_SIZE,
                          inline={"": red})
    assert w_plain == w_inline == ["ab"]  # одна строка в обоих случаях


def test_unknown_pua_skipped_no_crash():
    out = render_quote_png(body="hi  bye", sender_name="N", sender_id=1)
    assert out is not None and len(out) > 1000


def test_extended_emoji_ranges_kept():
    # стрелки/фигуры больше не пропадают и не крашат wrap
    out = render_quote_png(body="a → b ▶ c #️⃣", sender_name="N", sender_id=1)
    assert out is not None and len(out) > 1000
