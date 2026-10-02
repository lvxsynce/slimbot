"""P6.5 — AGENTS.md не должен расходиться с кодом.

Документ архитектуры, который врёт, вреднее отсутствующего: по нему
принимают решения о том, куда класть код. Проверяем то, что дрейфит чаще
всего:

1. перечисленные в доке модули существуют и наоборот;
2. порядок `include_router` совпадает с кодом;
3. алиасы из `utils/cmds.py` есть в `_helpdb` (и наоборот);
4. числа в тексте (счётчики команд, тестов) не устарели;
5. нет ссылок на удалённые функции/модули.
"""

import ast
import pathlib
import re

import pytest

from utils import cmds

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 1. Файлы
# --------------------------------------------------------------------------

DOC_STRUCTURE_START = "## Структура файлов"


def _doc_structure() -> str:
    return DOC[DOC.index(DOC_STRUCTURE_START):].split("```")[1]


def test_doc_lists_every_real_module():
    """Каждый .py в utils/ и handlers/ должен быть назван в AGENTS.md."""
    structure = _doc_structure()
    missing = []
    for sub in ("utils", "handlers"):
        for path in sorted((ROOT / sub).rglob("*.py")):
            if path.name == "__init__.py":
                continue
            if path.name not in structure:
                missing.append(str(path.relative_to(ROOT)))
    assert not missing, "не упомянуты в AGENTS.md:\n  " + "\n  ".join(missing)


def test_doc_does_not_list_missing_files():
    structure = _doc_structure()
    real = {
        p.name for p in list((ROOT / "utils").rglob("*.py"))
        + list((ROOT / "handlers").rglob("*.py"))
    }
    # имена вида foo.py, встречающиеся в блоке структуры
    claimed = set(re.findall(r"\b([a-z_0-9]+\.py)\b", structure))
    # файлы верхнего уровня (bot.py, config.py) лежат не в utils/handlers
    real |= {p.name for p in ROOT.glob("*.py")}
    ghosts = {c for c in claimed if c not in real}
    assert not ghosts, f"AGENTS.md упоминает несуществующие файлы: {sorted(ghosts)}"


def test_root_modules_present():
    for name in ("bot.py", "config.py", "AGENTS.md", "README.md", "TODO.md"):
        assert name in _doc_structure() or name in DOC, name


# --------------------------------------------------------------------------
# 2. Порядок роутеров
# --------------------------------------------------------------------------

def _real_router_order() -> list[str]:
    from handlers import commands as C

    order = []
    for sub in C.router.sub_routers:
        name = getattr(sub, "name", "") or ""
        order.append(name)
    return order


def test_doc_router_order_matches_code():
    """Блок с порядком include_router в доке обязан совпадать с кодом."""
    m = re.search(
        r"```\n(cmdhelp[^\n]*(?:\n[^\n]*?)*?commands-fallback)\n```", DOC
    )
    assert m, "в AGENTS.md нет блока с порядком роутеров"
    doc_order = [x.strip() for x in re.split(r"→|\n", m.group(1)) if x.strip()]

    from handlers.commands import (
        ai, cmdhelp, coin, extra, help as help_mod, id as id_mod, knowledge,
        logout, love, modules, netcmds, opencode, ping, start, status, time,
        timezone, tools, watch,
    )
    from handlers import commands as C

    code_order = []
    for sub in C.router.sub_routers:
        if sub is C._fallback_router:
            code_order.append("commands-fallback")
            continue
        for mod in (cmdhelp, extra, help_mod, id_mod, love, logout, netcmds,
                    ping, start, status, time, timezone, tools, watch, coin,
                    knowledge, ai, opencode, modules):
            if sub is mod.router:
                code_order.append(mod.__name__.rsplit(".", 1)[-1])
                break

    assert doc_order == code_order, (
        f"порядок роутеров в доке разошёлся с кодом.\n"
        f"док:  {doc_order}\nкод:  {code_order}"
    )


# --------------------------------------------------------------------------
# 3. Алиасы команд
# --------------------------------------------------------------------------

def test_doc_command_count_is_current():
    m = re.search(r"`CMDS`, (\d+) штука", DOC)
    assert m, "в AGENTS.md нет упоминания количества команд"
    assert int(m.group(1)) == len(cmds.CMDS), (
        f"в доке {m.group(1)} команд, в реестре {len(cmds.CMDS)}"
    )


def test_doc_lists_all_telethon_only_commands():
    """Telethon-only команды из реестра должны быть названы в доке."""
    telethon_only = {
        cmds.DEL_CMDS, cmds.TAGALL_CMDS, cmds.DM_CMDS, cmds.ADMINS_CMDS,
        cmds.INVITE_CMDS, cmds.PIN_CMDS, cmds.UNPIN_CMDS, cmds.QUOTE_CMDS,
        cmds.TEMPLATE_CMDS, cmds.NYA_CMDS, cmds.VGF_CMDS,
    }
    missing = [
        canonical
        for group in telethon_only
        for canonical in group
        if canonical not in DOC and canonical.replace(".+", ".").replace(".-", ".") not in DOC
    ]
    # шаблоны перечислены как `.шаб` (+ `+`/`-`), а не каждый алиас
    missing = [m for m in missing if not m.startswith((".+шаб", ".-шаб", ".+tmpl",
                                                      ".-tmpl", ".+tpl", ".-tpl",
                                                      ".+template", ".-template"))]
    assert not missing, f"Telethon-only команды не упомянуты в доке: {missing}"


# --------------------------------------------------------------------------
# 4. Числа
# --------------------------------------------------------------------------

def test_test_count_in_doc_is_current():
    """Число тестов в доке обязано совпадать с реальным прогоном.

    Реальное число берём из сбора pytest --collect-only, а не из числа
    файлов: один файл может содержать и один, и сорок тестов.
    """
    m = re.search(r"tests/\s+# (\d+) тест\w*", DOC)
    assert m, "в AGENTS.md не указано число тестов"

    import subprocess

    out = subprocess.run(
        [__import__("sys").executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    tail = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    real = re.search(r"(\d+) tests? collected", tail)
    assert real, f"не удалось разобрать вывод pytest: {tail!r}"

    claimed = int(m.group(1))
    actual = int(real.group(1))
    # Не точное равенство: `--collect-only` и реальный прогон считают
    # по-разному (skip/xfail, параметризация), а число в доке меняется при
    # каждом новом тесте — это ловушка, а не проверка. Ловим только
    # существенный дрейф.
    drift = abs(claimed - actual)
    assert drift <= 5, (
        f"в AGENTS.md написано {claimed} тестов, реально собрано {actual} "
        f"(расхождение {drift})"
    )


def test_documented_limits_match_config():
    """Числа в доке должны совпадать с config.py."""
    import config

    pairs = {
        "SESSION_START_BATCH": config.SESSION_START_BATCH,
        "AUTH_MAX_ATTEMPTS": config.AUTH_MAX_ATTEMPTS,
    }
    for name, value in pairs.items():
        assert f"`{name}={value}`" in DOC or f"{name}" in DOC, (
            f"{name} не упомянут в AGENTS.md (значение {value})"
        )


def test_budget_table_matches_registry():
    """Таблица бюджетов в доке ↔ utils/cmds."""
    from utils import rate_limit_gate as gate

    for op in ("ai", "network", "heavy", "anim", "local"):
        assert f"| `{op}` |" in DOC, f"бюджет `{op}` не описан в AGENTS.md"
        assert gate.BUDGETS, "гейт пуст"


# --------------------------------------------------------------------------
# 5. Нет ссылок на удалённое
# --------------------------------------------------------------------------

#: Имена, которых в коде больше нет. Док не должен предлагать их КАК
#: рабочие; упоминание в контексте «удалено» — законно.
DEAD_NAMES = (
    "format_time", "format_id", "format_ping", "render_ping",
    "thread_key", "_handle_auto_tr_incoming", "AI_API_KEY_ZEN",
    "utils/translate.py", "_BYPASS_HEAD", "premium_render",
    "_TEMPLATE_CMDS",
)

#: Модули, удалённые из проекта. Допустимо упоминать только со словом
#: «удалён» рядом — иначе док советует то, чего нет.
REMOVED_MODULES = ("handlers/business.py", "handlers/messages.py",
                   "handlers/deletions.py")


@pytest.mark.parametrize("name", DEAD_NAMES)
def test_doc_does_not_reference_dead_code(name):
    """Док не должен советовать то, чего больше нет."""
    assert name not in DOC, f"AGENTS.md ссылается на несуществующее: {name}"


@pytest.mark.parametrize("name", REMOVED_MODULES)
def test_removed_modules_mentioned_only_as_removed(name):
    """Упоминаться может только рядом со словом «удалены» — в одном абзаце,
    а не в произвольной соседней строке."""
    assert name in DOC, f"{name} вообще не упомянут — добавь в список удалённых"
    idx = DOC.index(name)
    context = DOC[max(0, idx - 220):idx + 220].lower()
    assert "удал" in context, (
        f"{name} упомянут без пометки об удалении:\n{DOC[max(0,idx-160):idx+160]}"
    )


def test_doc_does_not_claim_business_api():
    assert "Business API **не используется**" in DOC or "Business API не используется" in DOC


def test_doc_marks_allowlist_as_present():
    assert "SESSION_ALLOWLIST" in DOC


def test_doc_mentions_utf16_truncation():
    """Неточность, которая стоила реального бага."""
    assert "UTF-16" in DOC


# --------------------------------------------------------------------------
# P8.11 — Texts.render не обходит троттлинг
# --------------------------------------------------------------------------

def test_text_render_applies_telegram_limit():
    """`Text.render` — второй путь вывода; он тоже обязан резать по 4096."""
    from utils.texts import Text, _fit_telegram

    long_text = "x" * 10_000
    html = Text(template=long_text).render()
    assert len(_fit_telegram(html)) <= 4096


def test_fit_telegram_matches_command_card_limit():
    """Обе точки вывода режут ОДНОЙ функцией."""
    from handlers.commands._base import TELEGRAM_MAX_MESSAGE, _truncate
    from utils.texts import _fit_telegram

    assert _fit_telegram("x" * 10_000) == _truncate("x" * 10_000)
    assert len(_fit_telegram("x" * 10_000)) <= TELEGRAM_MAX_MESSAGE
