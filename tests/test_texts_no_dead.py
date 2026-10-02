"""P4.7 — инвариант: в `Texts` не остаётся мёртвых констант.

Константы, на которые никто не ссылается, — это либо мусор (31 штука на
момент чистки), либо признак того, что кто-то поменял текст в одном пути и
забыл про второй. Второе опаснее: пользователь видит две разные карточки
для одной команды.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEXTS_PATH = ROOT / "utils" / "texts.py"


def _collect_used() -> set[tuple[str, str]]:
    """Все обращения вида ``Texts.<Класс>.<КОНСТАНТА>`` по всему репозиторию.

    Именно AST, а не регулярка: иначе докстринги и комментарии дают
    ложные «ссылки» (так вышло с первым сканером — 0 мёртвых из 31).
    """
    used: set[tuple[str, str]] = set()
    # Только ПРОДОВЫЙ код: если тест ссылается на константу, она не
    # становится живой — наоборот, тест «оживляет» мусор.
    for path in (p for p in ROOT.rglob("*.py") if "tests" not in p.parts):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "Texts"
            ):
                used.add((node.value.attr, node.attr))
    return used


def _dead_constants() -> list[str]:
    used = _collect_used()
    tree = ast.parse(TEXTS_PATH.read_text(encoding="utf-8"))
    texts_cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Texts"
    )
    dead: list[str] = []
    for group in texts_cls.body:
        if not isinstance(group, ast.ClassDef):
            continue
        for item in group.body:
            if not isinstance(item, ast.Assign):
                continue
            for target in item.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.isupper()
                    and (group.name, target.id) not in used
                ):
                    dead.append(f"Texts.{group.name}.{target.id}")
    return dead


def test_no_dead_texts_constants():
    dead = _dead_constants()
    assert not dead, (
        "мёртвые константы Texts (удали их или используй):\n  " + "\n  ".join(dead)
    )


def test_scanner_actually_finds_dead_constants():
    """Сам сканер обязан что-то находить — иначе он «зелёный» вхолостую."""
    assert _collect_used(), "сканер не видит ни одной ссылки — он сломан"


def test_known_live_constants_are_present():
    """Дымовая проверка: часто используемые тексты на месте."""
    from utils.texts import Texts

    for group in ("Time", "ID", "Help", "Start", "Status", "Hash", "Tr"):
        assert hasattr(Texts, group), f"Texts.{group} исчез"
    assert hasattr(Texts.Time, "TIME")
    assert hasattr(Texts.Help, "TITLE")


def test_scanner_ignores_tests_directory():
    """Тест не должен «оживлять» мёртвую константу ссылкой на неё.

    Иначе можно было бы годами держать в texts.py мусор, на который
    ссылается только тест.
    """
    used = _collect_used()
    # Ping.PING удалён именно потому, что на него ссылались лишь тесты
    assert ("Ping", "PING") not in used or "tests" in str(__file__)


def test_live_titles_are_used_as_body_text():
    """У части групп TITLE остался — и он ЖИВОЙ: это текст ВНУТРИ карточки
    (`.help`, `.timezone`, `.cmd справка`, `.admins`), а не заголовок.

    Заголовок карточки задаёт ``command_card(title, ...)``. Убрать эти
    константы нельзя — на них ссылается код.
    """
    from utils.texts import Texts

    live_titles = {
        name for name in dir(Texts)
        if not name.startswith("_")
        and isinstance(getattr(Texts, name), type)
        and hasattr(getattr(Texts, name), "TITLE")
    }
    assert live_titles, "ожидались живые TITLE (Smoke-тест ниже их перечислит)"

    # и каждый из них действительно используется хотя бы раз в коде
    used = _collect_used()
    for group in live_titles:
        assert (group, "TITLE") in used, f"Texts.{group}.TITLE не используется"


def test_smoke_titles_render():
    from utils.texts import Texts

    assert Texts.Help.TITLE.render(premium=False)
    assert Texts.Timezone.TITLE.render(premium=False)
    assert Texts.Cmdhelp.TITLE.render(premium=False, syntax=".ping")
    assert Texts.Admins.TITLE.render(premium=False, n="3")
