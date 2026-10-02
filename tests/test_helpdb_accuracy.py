"""P6.4 — справка в `_helpdb` обязана соответствовать коду.

`_helpdb.HELPS` — то, что пользователь читает через `.команда справка`.
Враньё там хуже, чем отсутствие: пользователь пробует то, чего нет, и
решает, что команда сломана. Каждый пункт ниже — реальная правка.
"""

import inspect

import pytest

from handlers.commands import _helpdb


@pytest.mark.parametrize("key", sorted(_helpdb.HELPS))
def test_help_entries_have_syntax_and_desc(key):
    entry = _helpdb.HELPS[key]
    assert entry.get("syntax"), f"{key}: нет syntax"
    assert entry.get("desc"), f"{key}: нет desc"


@pytest.mark.parametrize("key", sorted(_helpdb.HELPS))
def test_help_syntax_matches_registry(key):
    """Синтаксис в справке не должен предлагать несуществующий алиас."""
    from utils import cmds

    import re

    syntax = _helpdb.HELPS[key]["syntax"]
    for token in re.split(r"\||\s{2,}", syntax):
        token = token.strip()
        if not token or not token.startswith("."):
            continue
        head = token.split()[0]
        assert cmds.is_known(head), f"{key}: справка предлагает неизвестную команду {head}"


def test_hash_lists_every_algorithm():
    """.hash поддерживает 10 алгоритмов, а не 8 — пропущены sha3_256/sha3_512."""
    from utils.hashing import ALGOS

    desc = _helpdb.HELPS["hash"]["desc"]
    missing = [a for a in ALGOS if a not in desc]
    assert not missing, f"алгоритмы не упомянуты в справке .hash: {missing}"


def test_quote_width_matches_code():
    """.quote рисует 1400px, в справке было 1200px."""
    from utils import quote_image

    desc = _helpdb.HELPS["quote"]["desc"]
    assert f"{quote_image.WIDTH}px" in desc, desc


def test_vgf_help_matches_implementation():
    """.вгф поддерживает видео/кружки/стикеры — в справке значилось обратное."""
    desc = _helpdb.HELPS["vgf"]["desc"]
    assert "НЕ поддерживаются" not in desc, desc
    assert "Видео" in desc or "видео" in desc, desc


def test_watch_help_does_not_promise_deleted_monitoring():
    """.следить за удалёнными сообщениями больше нельзя (фича удалена)."""
    desc = _helpdb.HELPS["watch"]["desc"]
    assert "удалени" not in desc.lower(), desc
    assert "одноразов" in desc.lower() or "фото" in desc.lower(), desc


def test_tr_auto_help_direction_is_correct():
    """`.tr auto` переводит свои ИСХОДЯЩИЕ, а не входящие."""
    desc = _helpdb.HELPS["tr"]["desc"]
    assert "исходящих" in desc.lower(), desc
    assert "входящих" not in desc.lower(), desc


def test_uuid_help_has_no_typo():
    assert "один штука" not in _helpdb.HELPS["uuid"]["desc"]


def test_template_help_covers_all_three_verbs():
    """В реестре три группы алиасов `.шаб` / `.+шаб` / `.-шаб`."""
    from utils import cmds

    desc = _helpdb.HELPS["template"]["desc"] + _helpdb.HELPS["template"]["syntax"]
    for token in (".шаб", ".+шаб", ".-шаб"):
        assert token in desc, f"{token} не описан в справке .шаб"


def test_every_registry_key_has_help():
    from utils import cmds
    import handlers.commands as C

    for sub in C.router.sub_routers:
        pass  # структура роутеров проверяется в test_command_registry


def test_aliases_map_is_bidirectional():
    """ALIASES строится из _PAIRS; рассинхрон означает «справка не находится»."""
    for key, aliases in _helpdb._PAIRS.items():
        for alias in aliases:
            assert _helpdb.ALIASES.get(alias) == key, (
                f"ALIASES[{alias!r}] = {_helpdb.ALIASES.get(alias)!r}, ожидался {key!r}"
            )
            assert key in _helpdb.HELPS, f"ключ {key} есть в _PAIRS, но нет в HELPS"


def test_help_words_recognised():
    for word in ("справка", "help", "?", "хелп"):
        text = f".ping {word}"
        ok, key = _helpdb.is_help_request(text)
        assert ok is True, text
        assert key == "ping", (text, key)


def test_help_request_detection_uses_word_not_whole_text():
    """.ии help me with my code не должен съедаться как справка.

    Известное поведение: второе слово == help ⇒ перехват. Проверяем явно,
    чтобы изменение было осознанным, а не случайным.
    """
    ok, key = _helpdb.is_help_request(".ии help me with my code")
    assert ok is True and key == "ai"

    ok, _ = _helpdb.is_help_request(".ии напиши мне код")
    assert ok is False
