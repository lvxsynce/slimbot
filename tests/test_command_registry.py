"""P4.4 / P7.2 — единый реестр команд вместо десяти разных списков.

Дрейф был реальным: `_handle_outgoing` держал свой хардкод-кортеж, каждая
команда — свой `*_CMDS` + мёртвый `_check()`, и guard-тесты были только у
двух модулей. Теперь источник алиасов ровно один — `utils.cmds`.
"""

import ast
import inspect
import pathlib

import pytest

from utils import cmds
from utils.telethon_manager import TelethonManager

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Модули, которые ДОЛЖНЫ импортировать алиасы из реестра.
#: `delmsg` исключён: `.del` разбирает аргументы напрямую и алиасы не
#: использует — сверка идёт по самому реестру (test_helpdb_agrees_with_registry).
TELETHON_ONLY = [
    ("handlers/commands/dm.py", "DM_CMDS"),
    ("handlers/commands/tagall.py", "TAGALL_CMDS"),
    ("handlers/commands/admins.py", "ADMINS_CMDS"),
    ("handlers/commands/invitelink.py", "INVITE_CMDS"),
    ("handlers/commands/vgf.py", "VGF_CMDS"),
    ("handlers/commands/nya.py", "NYA_CMDS"),
    ("handlers/commands/quote.py", "QUOTE_CMDS"),
]


# --------------------------------------------------------------------------
# Модули команд берут алиасы из реестра, а не хранят свои
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rel,const", TELETHON_ONLY)
def test_module_imports_aliases_from_registry(rel, const):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert f"from utils.cmds import {const}" in src, (
        f"{rel} must import {const} from utils.cmds"
    )


@pytest.mark.parametrize("rel,const", TELETHON_ONLY)
def test_module_does_not_define_its_own_tuple(rel, const):
    """Константа не переопределяется локально — иначе снова два источника."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    defined = {
        t.id for n in tree.body if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    assert const not in defined, f"{rel} redefines {const} instead of using the registry"


@pytest.mark.parametrize("rel,const", TELETHON_ONLY)
def test_dead_check_guards_are_gone(rel, const):
    """Мёртвый `_check()` больше не нужен — вызовов у него не было."""
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "def _check(" not in src, f"{rel}: dead _check() guard is back"


# --------------------------------------------------------------------------
# Диспатч использует реестр
# --------------------------------------------------------------------------

def test_dispatch_uses_registry_not_hardcoded_tuples():
    src = inspect.getsource(TelethonManager._handle_outgoing)
    # Никаких хардкод-кортежей с командами
    assert 'head in ("' not in src, "hardcoded command tuples are back in _handle_outgoing"
    # Вместо них — константы реестра
    for const in ("PING_CMDS", "TR_CMDS", "DEL_CMDS", "QUOTE_CMDS", "TEMPLATE_CMDS"):
        assert f"head in {const}" in src, f"{const} not used in dispatch"


def test_rate_limit_uses_registry():
    """Диспатч обязан звать единый гейт, а не собирать списки руками."""
    src = inspect.getsource(TelethonManager._handle_outgoing)
    assert "gate.is_limited(head)" in src
    assert "gate.check(head, user_id)" in src
    # и никаких хардкод-кортежей с командами
    assert 'head in ("' not in src


def test_template_dispatch_matches_registry():
    """Старый тест ловил дрейф только для `.шаб`. Теперь — для всех."""
    from utils.cmds import TEMPLATE_CMDS

    src = inspect.getsource(TelethonManager._handle_outgoing)
    assert "head in TEMPLATE_CMDS" in src
    assert ".+шаб" in TEMPLATE_CMDS and ".-шаб" in TEMPLATE_CMDS


# --------------------------------------------------------------------------
# Реестр самосогласован
# --------------------------------------------------------------------------

def test_no_duplicate_aliases_in_full_list():
    seen = {}
    dupes = []
    for cmd in cmds.CMDS:
        if cmd in seen:
            dupes.append(cmd)
        seen[cmd] = True
    assert not dupes, f"duplicate aliases: {dupes}"


def test_budgets_are_subset_of_cmds():
    assert set(cmds.CMDS_AI) <= set(cmds.CMDS)
    assert set(cmds.CMDS_NETWORK) <= set(cmds.CMDS)


def test_ai_and_network_budgets_do_not_overlap():
    overlap = cmds.AI_BUDGET & cmds.NETWORK_BUDGET
    assert not overlap, f"a command is in both budgets: {overlap}"


def test_is_known():
    assert cmds.is_known(".ping")
    assert cmds.is_known("  .PING  ")
    assert not cmds.is_known(".nope")
    assert not cmds.is_known("")


def test_registry_is_leaf_module():
    """Реестр обязан быть листовым — иначе вернётся циклический импорт."""
    tree = ast.parse((ROOT / "utils" / "cmds.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            module = getattr(node, "module", "") or ""
            assert not module.startswith(("utils", "handlers", "config")), (
                f"utils.cmds must not import project modules, got {module}"
            )


def test_helpdb_agrees_with_registry():
    """`_helpdb` — источник per-command справки; он тоже не должен разойтись.

    Slash-команды (`.logout` в `_PAIRS` — это алиас `/logout`) в реестре
    хранятся со слэшем, поэтому сравниваем нормализованно.
    """
    from handlers.commands import _helpdb

    missing = []
    for key, aliases in _helpdb._PAIRS.items():
        for alias in aliases:
            head = alias if alias.startswith(".") else f".{alias}"
            alt = f"/{head[1:]}"
            if not (cmds.is_known(head) or cmds.is_known(alt)):
                missing.append((key, head))
    assert not missing, f"aliases in _helpdb but not in registry: {missing}"


def test_every_command_in_registry_is_documented_in_helpdb():
    """Обратная проверка: команда есть в реестре, но справки нет."""
    from handlers.commands import _helpdb

    known_in_help = set()
    for aliases in _helpdb._PAIRS.values():
        for alias in aliases:
            known_in_help.add(alias if alias.startswith(".") else f".{alias}")
    missing = sorted(set(cmds.CMDS) - known_in_help)
    assert not missing, f"commands without per-command help: {missing}"
