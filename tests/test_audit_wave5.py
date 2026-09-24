"""Тесты фиксов аудита, волна 5: calc, storage, escape."""

import pytest

from utils.calc import calc
from utils import storage
from utils.escape import sanitize_llm_html


def test_calc_pow_function_precheck():
    with pytest.raises(ValueError):
        calc("pow(9, pow(9, 9))")
    with pytest.raises(ValueError):
        calc("pow(2, 100000)")
    assert calc("pow(2, 10)") == "1024"
    assert calc("pow(2, 10, 100)") == "24"
    with pytest.raises(ValueError):
        calc("9**9**9")


def test_calc_basics_still_work():
    assert calc("2 + 2 * 3") == "8"
    assert calc("hex(255)") == "0xff"
    with pytest.raises(ValueError):
        calc("factorial(10**7)")


def test_migrate_watched_skips_bad_elements():
    out = storage._migrate_watched([[-100, 0], ["abc", 0], 42, None, [-101, "x"]])
    assert [-100, 0] in out
    assert [42, 0] in out
    assert [-101, 0] in out
    assert len(out) == 3


def test_photo_allowed_corrupt_record_no_crash():
    storage.photo_settings["u5corrupt"] = {}
    storage.photo_settings["u5bad"] = {"enabled": True}
    try:
        assert storage.is_photo_allowed("u5corrupt", -100, 0) is False
        assert storage.is_photo_allowed("u5bad", -100, 0) is True
    finally:
        storage.photo_settings.pop("u5corrupt", None)
        storage.photo_settings.pop("u5bad", None)


def test_sanitize_strips_attrs_on_plain_tags():
    out = sanitize_llm_html('<b onclick="x">hi</b>')
    assert "<b onclick" not in out  # живой тег с атрибутами не проходит
    assert "hi" in out
    out2 = sanitize_llm_html('<code class="y">c</code>')
    assert "<code class" not in out2
    ok = sanitize_llm_html('<b>hi</b> <a href="https://x.ru">l</a>')
    assert "<b>hi</b>" in ok
    assert '<a href="https://x.ru">' in ok


def test_sanitize_keeps_hex_entities():
    assert "&#x41;" in sanitize_llm_html("a&#x41;b")
    assert "&#X4a;" in sanitize_llm_html("a&#X4a;b")
    assert "&amp;" in sanitize_llm_html("a&b")


def test_new_aliases_dispatched():
    from handlers.commands.tools import TR_CMDS, _tr_check
    from handlers.commands.netcmds import HASH_CMDS, _is

    assert ".перевести" in TR_CMDS
    assert ".хэш" in HASH_CMDS
    assert _tr_check(".перевести en hi") is True
    assert _is(HASH_CMDS, ".хэш sha256 x") is True


def test_helpdb_single_opencode_key():
    from handlers.commands._helpdb import HELPS

    assert list(HELPS).count("opencode") == 1
    assert "моделям" in HELPS["opencode"]["desc"]


def test_quote_wrap_breaks_long_words():
    from utils.quote_image import _wrap_text

    class Font:
        def getlength(self, s):
            return len(s) * 10.0

    lines = _wrap_text("abc " + "x" * 100 + " def", Font(), 200)
    assert all(Font().getlength(line) <= 200 for line in lines)
    assert "".join(lines).replace(" ", "") == "abc" + "x" * 100 + "def"


def test_hash_uses_caption():
    import asyncio
    from types import SimpleNamespace
    from handlers.commands import netcmds

    reply = SimpleNamespace(
        text=None, caption="подпись", document=None, photo=None,
        video=None, audio=None, voice=None, video_note=None, sticker=None,
    )
    msg = SimpleNamespace(reply_to_message=reply, bot=None)
    out = asyncio.run(netcmds._do_hash("u5", "", msg))
    import hashlib

    assert hashlib.sha256("подпись".encode()).hexdigest() in out


def test_pillow_bomb_guard_set():
    import utils  # noqa: F401
    try:
        from PIL import Image
    except ImportError:
        return
    assert Image.MAX_IMAGE_PIXELS <= 50_000_000


def test_hash_abort_callback():
    from utils.telethon_manager import _abort_big_download, _TooBigDownload, HASH_DOWNLOAD_MAX_BYTES
    import pytest

    _abort_big_download(100, 1000)
    with pytest.raises(_TooBigDownload):
        _abort_big_download(HASH_DOWNLOAD_MAX_BYTES + 1, 0)
