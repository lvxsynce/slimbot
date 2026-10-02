"""P3.10 / P4 — мёртвый код удалён, и его больше не вернуть.

Закрывает:
- `premium_render` — kwarg в трёх форматтерах Telethon, который НИ РАЗУ не
  читался в теле функции, при этом docstring'и утверждали, что он управляет
  рендером эмодзи, а четыре вызова передавали `is_entity_premium(...)`.
- `format_time` / `format_id` — sync-версии рендера без единого вызова.
- `storage.thread_key` — ни одного вызова, но упомянут в AGENTS.md дважды
  как живой хелпер.
- `_BYPASS_HEAD = ()` — вечно пустой кортеж с мёртвой проверкой.
"""

import ast
import inspect
import pathlib

import pytest

from handlers.commands import _base
from utils import storage as S
from utils import telethon_manager as TLM

ROOT = pathlib.Path(__file__).resolve().parent.parent

FORMATTERS = ["_format_me_telethon", "_format_chat_telethon", "_format_who_telethon"]


# --------------------------------------------------------------------------
# premium_render
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", FORMATTERS)
def test_premium_render_parameter_is_gone(name):
    fn = getattr(TLM, name)
    params = list(inspect.signature(fn).parameters)
    assert "premium_render" not in params, (
        f"{name}: premium_render was a no-op parameter and must stay removed"
    )


@pytest.mark.parametrize("name", FORMATTERS)
def test_formatter_docstring_does_not_claim_premium_render(name):
    fn = getattr(TLM, name)
    doc = inspect.getdoc(fn) or ""
    if "premium_render" in doc:
        # Допустимо только явное упоминание «удалён»
        assert "удалён" in doc or "удалено" in doc, (
            f"{name}: docstring mentions premium_render without saying it was removed"
        )


def test_no_sender_premium_computation_remains():
    src = (ROOT / "utils" / "telethon_manager.py").read_text(encoding="utf-8")
    assert "sender_premium" not in src, (
        "sender_premium only existed to feed the dead premium_render param"
    )


def test_is_entity_premium_import_removed():
    src = (ROOT / "utils" / "telethon_manager.py").read_text(encoding="utf-8")
    assert "is_entity_premium" not in src.split('"""')[0] or True
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "utils.premium":
            imported |= {a.name for a in node.names}
    assert "is_entity_premium" not in imported, "unused import must be removed"


def test_formatters_still_render():
    """Удаление параметра не должно сломать сам рендер."""
    ent = type("U", (), {
        "id": 1, "first_name": "Иван", "last_name": "П", "username": "ivan",
        "phone": "+7999", "lang_code": "ru", "premium": True,
        "verified": True, "bot": False, "scam": False, "fake": False,
        "restricted": False, "support": False, "deleted": False,
        "mutual_contact": False, "contact": False, "status": None,
    })()
    assert "Иван" in TLM._format_who_telethon(ent)
    assert "Иван" in TLM._format_me_telethon(ent)
    chat = type("C", (), {
        "title": "Чат", "id": -1001, "participants_count": 5, "verified": False,
        "scam": False, "fake": False, "restricted": False, "username": None,
    })()
    assert "Чат" in TLM._format_chat_telethon(chat, -1001)


# --------------------------------------------------------------------------
# format_time / format_id
# --------------------------------------------------------------------------

def test_sync_format_helpers_are_removed():
    assert not hasattr(_base, "format_time"), "format_time had zero callers"
    assert not hasattr(_base, "format_id"), "format_id had zero callers"
    assert not hasattr(_base, "format_ping"), "format_ping had zero callers"


def test_render_ping_is_gone():
    """`.ping` переведён на общий форматтер utils.shared_cmd.ping_body."""
    assert not hasattr(_base, "render_ping")


def test_no_module_imports_removed_helpers():
    for path in (ROOT / "handlers").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for dead in ("format_time", "format_id", "format_ping"):
            assert dead not in src, f"{path.relative_to(ROOT)} still references {dead}"


def test_async_render_helpers_still_exist():
    for name in ("render_time", "render_id", "render_help"):
        assert hasattr(_base, name), f"{name} must stay"


# --------------------------------------------------------------------------
# thread_key
# --------------------------------------------------------------------------

def test_thread_key_is_removed():
    assert not hasattr(S, "thread_key"), "thread_key had zero callers"


def test_no_reference_to_thread_key():
    root = ROOT / "utils"
    for path in root.rglob("*.py"):
        assert "thread_key" not in path.read_text(encoding="utf-8"), path


# --------------------------------------------------------------------------
# _BYPASS_HEAD
# --------------------------------------------------------------------------

def test_bypass_head_is_removed():
    from handlers import commands as C
    assert not hasattr(C, "_BYPASS_HEAD")


def test_bypass_is_exact_match_only():
    from handlers import commands as C
    assert C._bypass(".help")
    assert C._bypass(".ПОМОЩЬ")
    assert not C._bypass(".ping")
    assert not C._bypass(".helpx")


# --------------------------------------------------------------------------
# Общий инвариант: в проекте нет неиспользуемых локальных импортов
# --------------------------------------------------------------------------

def test_no_unused_module_level_imports_in_hot_modules():
    """Простая эвристика: импорт X, который больше нигде в модуле не встречается."""
    targets = [
        "utils/telethon_manager.py",
        "handlers/commands/_base.py",
        "handlers/commands/watch.py",
        "handlers/commands/admins.py",
        "handlers/commands/opencode.py",
        "utils/ai_memory.py",
        "utils/knowledge_db.py",
        "utils/rate_limit.py",
    ]
    problems = []
    for rel in targets:
        src = (ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported[(a.asname or a.name).split(".")[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported[a.asname or a.name] = node.lineno
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                v = node.value
                if isinstance(v, ast.Name):
                    used.add(v.id)
        # имена, встречающиеся в строках (аннотации/докстроки), не считаем
        for name, lineno in imported.items():
            if name not in used and name not in src.split("import")[0]:
                # грубая проверка по тексту: вдруг это в аннотации
                occurrences = src.count(name)
                if occurrences <= 1:
                    problems.append(f"{rel}:{lineno} unused import {name}")
    assert not problems, "\n".join(problems)
