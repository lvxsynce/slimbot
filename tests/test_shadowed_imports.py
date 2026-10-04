"""Ловушка Python: локальный `import` затеняет модульный.

`from config import AI_CONTEXT_LEN_DEFAULT` внутри функции делает имя
**локальным** для всей функции, а не с момента выполнения импорта.
Любое обращение к имени выше по тексту функции даёт:

    UnboundLocalError: cannot access local variable 'X' where it is
    not associated with a value

Именно это ломало `.ии`: импорт стоял на строке 1447, а чтение — на
1427 и 450. Команда падала при ЛЮБОМ вызове, и тесты этого не видели:
достаточно было одного прямого вызова хендлера с путём, где ранний
`return` миновал проблемную строку.

Проверяем AST-ом: в каждой функции не должно быть имени, которое
(а) импортировано на уровне модуля, (б) импортировано ещё и внутри
этой функции, и (в) читается выше внутреннего импорта.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Пропускаем тесты (там такие конструкции законны — фикстуры
#: переопределяют импорт) и служебные каталоги.
def _sources():
    for path in sorted(ROOT.rglob("*.py")):
        parts = set(path.parts)
        if parts & {"tests", ".venv", "venv", "__pycache__", ".git"}:
            continue
        yield path


def _module_imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _local_import_line(fn: ast.AST, name: str) -> int | None:
    """Номер первой строки, где `name` импортируется внутри функции."""
    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return node.lineno
    return None


def _loads_before(fn: ast.AST, name: str, line: int) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Name) and n.id == name
        and isinstance(n.ctx, ast.Load)
        and n.lineno < line
    ]


def test_no_shadowed_module_import():
    """ГЛАВНАЯ проверка: ни один модульный импорт не затенён локальным."""
    problems: list[str] = []
    for path in _sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        top = _module_imports(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local: set[str] = set()
            for node in ast.walk(fn):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        local.add(alias.asname or alias.name.split(".")[0])
            for name in sorted(top & local):
                imp_line = _local_import_line(fn, name)
                if imp_line is None:
                    continue
                bad = _loads_before(fn, name, imp_line)
                if bad:
                    rel = path.relative_to(ROOT)
                    problems.append(
                        f"{rel}:{fn.lineno} {fn.name}() — '{name}' читается "
                        f"на строке {bad[0]}, но импортируется внутри "
                        f"функции только на {imp_line}"
                    )
    assert not problems, (
        "UnboundLocalError в проде: модульный импорт затенён локальным.\n  "
        + "\n  ".join(problems)
    )


def test_detector_actually_catches_the_bug():
    """Сам детектор должен ловить баг, иначе «зелёный» тест ничего не значит.

    Синтетический пример повторяет то, что было в `.ии`.
    """
    sample = '''
from config import SOME_SETTING


def handle():
    value = SOME_SETTING or 1
    from config import SOME_SETTING
    return value
'''
    tree = ast.parse(sample)
    top = _module_imports(tree)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef))
    local = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(fn) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert top & local, "детектор не заметил затенения"
    line = _local_import_line(fn, "SOME_SETTING")
    assert line == 7, "импорт внутри функции — строка 7"
    assert _loads_before(fn, "SOME_SETTING", line) == [6], "чтение на строке 6, выше импорта"


def test_clean_function_has_no_problem():
    """Импорт выше первого использования — корректный паттерн."""
    sample = '''
from utils.escape import esc


def ok(err):
    from utils.escape import esc
    return esc(err)
'''
    tree = ast.parse(sample)
    top = _module_imports(tree)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef))
    local = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(fn) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    name = next(iter(top & local))
    line = _local_import_line(fn, name)
    assert not _loads_before(fn, name, line)


def test_ai_handler_uses_module_level_context_default():
    """Регресс на конкретный баг `.ии`.

    `AI_CONTEXT_LEN_DEFAULT` импортируется на уровне модуля и НЕ должен
    переимпортироваться внутри функций: раньше такой импорт ломал
    `.ии` целиком.
    """
    src = (ROOT / "handlers" / "commands" / "ai.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    top = _module_imports(tree)
    assert "AI_CONTEXT_LEN_DEFAULT" in top, "константа должна импортироваться на модуле"
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    assert name != "AI_CONTEXT_LEN_DEFAULT", (
                        f"{fn.name}() переимпортирует AI_CONTEXT_LEN_DEFAULT "
                        "на строке "
                        f"{node.lineno} — это ломает чтение выше по функции"
                    )


def test_ai_handler_compiles_and_paths_are_reachable():
    """Хендлер должен импортироваться и иметь читаемые константы.

    Дёшево ловит класс «падает только в рантайме»: если модуль не
    импортируется, тест упадёт здесь, а не в проде.
    """
    import handlers.commands.ai as ai_mod
    from config import AI_CONTEXT_LEN_DEFAULT

    assert ai_mod.AI_CONTEXT_LEN_DEFAULT == AI_CONTEXT_LEN_DEFAULT
    assert callable(ai_mod.handle)