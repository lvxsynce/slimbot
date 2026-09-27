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


def test_quote_with_voice_attaches_voice():
    from handlers.commands.quote import handle

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
        voice = VoiceDoc()
        date = None
        fwd_from = None

        async def get_sender(self):
            return SimpleNamespace(id=42, username="vova", first_name="Вова",
                                   last_name="Петров", deleted=False)

        async def download_media(self, file=None):
            return b"voice-bytes"

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
    assert len(sent_files) == 2, sent_files
    png_file, png_kw = sent_files[0]
    voice_file, voice_kw = sent_files[1]
    assert "caption" not in png_kw  # цитата — только фотка, без текста
    assert voice_kw.get("voice_note") is True
    assert voice_kw.get("reply_to") == 777
    assert deleted == [True]


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


def test_body_wrapped_in_quotes():
    import utils.quote_image as qi
    seen = []
    orig = qi._draw_runs
    def spy(draw, img, x, y, text, font, emoji_px, fill, bold=False):
        seen.append(text)
        return orig(draw, img, x, y, text, font, emoji_px, fill, bold=bold)
    qi._draw_runs = spy
    try:
        render_quote_png(body="hello", sender_name="N", sender_id=1)
    finally:
        qi._draw_runs = orig
    assert seen and seen[0].startswith("«") is False  # имя без кавычек
    assert any("«" in s and "»" in s for s in seen)  # тело в кавычках


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
