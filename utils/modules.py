"""Загрузка и диспетчеризация пользовательских модулей.

Модуль — это `.py`-файл, который юзер присылает боту в личку. Он может
зарегистрировать:

* **dot-команды** — работают в любых чатах (Telethon-путь), как `.ping`;
* **inline-команды** — работают в inline-режиме, как `@bot статус`;
* **обработчики aiogram** (`@bot.message`) — только в личке с ботом.

Полная документация для авторов — `modules_dev.md`.

Модель доверия
--------------
Модуль исполняется **в процессе бота**. Песочницы нет: `import utils.*`,
`handlers.*`, `telethon_manager` доступны как обычно. Это осознанный
выбор — модуль должен «затрагивать основной функционал». Поэтому
единственная настоящая защита — гейт доступа (`MODULE_ALLOWLIST`),
проверяемый в трёх точках: при загрузке модуля, при вызове хендлера и
в UI. Всё остальное (`MODULES_MAX_BYTES`, проверка синтаксиса,
отсутствие конфликтов) — защита от ошибок и от опечаток, а не от
злоумышленника.

Изоляция мусора: каждый модуль импортируется под своим именем
`slimmod_<uid>_<slug>`, а при выгрузке из `sys.modules` вычищаются все
его подмодули. Иначе `importlib.reload` подхватил бы старый .py из
`__pycache__`.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import importlib.util
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from utils import storage
from utils.bot_info import bot_username_at

logger = logging.getLogger(__name__)

#: Префикс имени модуля в `sys.modules`. Менять нельзя — на нём держится
#: выгрузка (см. `_purge_modules`).
MODULE_PREFIX = "slimmod_"

#: Имя модуля → регулярка. Имя попадает в имя файла и в `sys.modules`,
#: поэтому допускаем только безопасный набор символов.
NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,32}$")

#: head команды: `.foo`, `.фу` — точка обязательна, дальше любые буквы/цифры/
#: подчёркивание (в т.ч. кириллица). Пробелов быть не может — head это
#: первый токен строки.
CMD_RE = re.compile(r"^\.[^\s.]{1,32}$")

#: inline-ключ: слово без точки, как `статус` в `@bot статус`.
INLINE_RE = re.compile(r"^[^\s.@]{1,32}$")


class ModuleError(Exception):
    """Модуль не прошёл проверку. `code` — короткий код для UI."""


# --------------------------------------------------------------------------
# Модель
# --------------------------------------------------------------------------


@dataclass
class CommandSpec:
    """Одна dot-команда модуля.

    `handler(ctx)` — coroutine. `paths` — какие пути обрабатывает:
    ``"telethon"`` (любой чат), ``"bot"`` (личка с ботом), оба сразу.
    """

    head: str
    aliases: tuple[str, ...]
    handler: Callable[[Any], Awaitable[Any]]
    desc: str
    #: Имя команды из `_helpdb`-подобного реестра для `.cmd справка`.
    key: str
    #: Заменяет ли системную команду с тем же head (см. `overrides`).
    overrides_system: str | None = None
    #: Явно выключенная системная команда, которую модуль «замещает».
    hides: str | None = None


@dataclass
class InlineSpec:
    """Одна inline-команда модуля.

    `handler(ctx)` возвращает список `InlineQueryResult*` либо `None`,
    если модуль не хочет отвечать (тогда сработает встроенная справка).
    """

    key: str
    aliases: tuple[str, ...]
    handler: Callable[[Any], Awaitable[Any]]
    desc: str


@dataclass
class ModuleInfo:
    """Разобранный модуль: метаданные + зарегистрированные команды."""

    name: str
    user_id: str
    path: Path
    title: str = ""
    version: str = "0"
    author: str = ""
    description: str = ""
    commands: list[CommandSpec] = field(default_factory=list)
    inline: list[InlineSpec] = field(default_factory=list)
    #: aiogram-роутер модуля (`router = Router()` в .py), либо None.
    router: Any = None
    loaded_at: float = 0.0
    error: str = ""

    @property
    def module_id(self) -> str:
        return f"{MODULE_PREFIX}{_hash_uid(self.user_id)}_{self.name}"


@dataclass
class Context:
    """То, что получает хендлер модуля.

    Сделано как dataclass, а не как `SimpleNamespace`, чтобы в IDE был
    автодополнёз, а в `modules_dev.md` можно было держать таблицу полей.
    """

    #: str(user_id) — тот, кто нажал команду.
    user_id: str
    #: Текст команды без head: `.note hello world` → `"hello world"`.
    args: str = ""
    #: Список аргументов (`args.split()`).
    argv: list[str] = field(default_factory=list)
    #: head команды в нижнем регистре: `.note`.
    head: str = ""
    #: Исходный Telethon event (`NewMessage`) либо aiogram `Message`.
    event: Any = None
    #: `TelegramClient` юзера (Telethon-путь) либо aiogram `Bot`.
    client: Any = None
    #: Модуль, который обрабатывает вызов.
    module: ModuleInfo | None = None
    #: Метаданные модуля: {"name", "version", "title", "description", "data"}.
    meta: dict = field(default_factory=dict)
    #: Пользовательское состояние модуля: `state.get/set/delete/all`.
    state: Any = None
    #: Поток в Telethon-пути (0 = не форум).
    thread_id: int = 0

    # -- помощники ---------------------------------------------------------

    def reply(self, text: str, *, html: bool = True, **kwargs) -> Any:
        """Ответить в тот же чат/топик. В Telethon-пути — `event.edit`."""
        event = self.event
        if event is None:
            return None
        if hasattr(event, "edit") and hasattr(event, "raw_text"):
            return event.respond(text, parse_mode="html" if html else None, **kwargs)
        return event.reply(text, parse_mode="html" if html else None, **kwargs)

    def edit(self, text: str, *, html: bool = True, **kwargs) -> Any:
        """Перезаписать сообщение с командой (обычный ответ бота)."""
        event = self.event
        if event is None:
            return None
        return event.edit(text, parse_mode="html" if html else None, **kwargs)

    async def reply_message(self) -> Any:
        """Сообщение, на которое юзер ответил, либо None."""
        event = self.event
        if event is None:
            return None
        getter = getattr(event, "get_reply_message", None)
        if getter is not None:
            return await getter()
        return getattr(event, "reply_to_message", None)


# --------------------------------------------------------------------------
# Реестр
# --------------------------------------------------------------------------

#: uid -> {module_name: ModuleInfo}
_registry: dict[str, dict[str, ModuleInfo]] = {}

#: head -> {uid: CommandSpec} — индекс для быстрого lookup'а.
#:
#: Именно словарь по uid, а не одна запись на head: два юзера вполне
#: могут поставить модули с одинаковым именем команды (`.note` у обоих),
#: и это НЕ конфликт. Плоский `head -> (uid, spec)` делал бы вызов
#: первым установленным и молча отдавал бы чужую команду.
_index: dict[str, dict[str, CommandSpec]] = {}

#: Явно отключённые системные команды: uid -> {head: True}.
_disabled_system: dict[str, set[str]] = {}

#: Что система уже знает о системных командах (для проверки конфликтов).
_system_heads: frozenset[str] = frozenset()


def _hash_uid(uid: str) -> str:
    return hashlib.sha256(str(uid).encode()).hexdigest()[:16]


def _cfg(name: str):
    """Читать конфиг модулей **в момент вызова**, а не на импорте.

    Импорт констант в теле модуля ломал бы и тесты (`monkeypatch` не
    подействует на уже скопированное значение), и эксплуатацию: смена
    `MODULES_*` без перезапуска тут невозможна, но хотя бы
    `importlib.reload(config)` подхватится.
    """
    import config

    return getattr(config, name)


def max_files() -> int:
    """Сколько модулей держит один юзер (`MODULES_MAX_FILES`)."""
    return int(_cfg("MODULES_MAX_FILES"))


def max_bytes() -> int:
    """Потолок на размер одного .py (`MODULES_MAX_BYTES`)."""
    return int(_cfg("MODULES_MAX_BYTES"))


def set_system_heads(heads) -> None:
    """Сообщить загрузчику системные команды.

    Вызывается из `bot.py::on_startup`. Без этого шага проверка конфликтов
    не знала бы, что `.ping` — системная команда, и модуль с `.ping`
    молча перехватил бы её.
    """
    global _system_heads
    _system_heads = frozenset(str(h).strip().lower() for h in heads)


def system_heads() -> frozenset[str]:
    return _system_heads


def modules_enabled() -> bool:
    return bool(_cfg("MODULES_ENABLED"))


def module_allowed(uid: str) -> bool:
    """Гейт доступа — копия `handlers.session.session_allowed`.

    Пустой `MODULE_ALLOWLIST` = открытый режим (WARNING пишет вызывающий).
    Непустой = только перечисленные id. Проверяется в трёх точках:
    загрузка модуля, вызов хендлера, UI (`/modules`).
    """
    allowlist = _cfg("MODULE_ALLOWLIST")
    if not allowlist:
        return True
    return str(uid) in allowlist


def allowlist_active() -> bool:
    return bool(_cfg("MODULE_ALLOWLIST"))


def list_modules(uid: str) -> dict[str, ModuleInfo]:
    """Загруженные модули юзера: {имя: ModuleInfo}."""
    return dict(_registry.get(str(uid), {}))


def list_files(uid: str) -> list[str]:
    """Файлы модулей юзера на диске (включая недогруженные)."""
    d = storage.module_dir(uid)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.py"))


def installed_names(uid: str) -> list[str]:
    """Всё, что «поставил» юзер: файлы ∪ метаданные."""
    return sorted(set(list_files(uid)) | set(storage.get_modules(uid)))


def find_command(uid: str, head: str) -> CommandSpec | None:
    """Хендлер dot-команды по head ИЛИ None.

    **Ключевое место изоляции.** `_index` хранит `(uid, spec)`, и проверка
    `owner_uid != str(uid)` — единственное, что не даёт команде одного
    юзера выполниться у другого. Проверять надо ДО возврата спеки, и
    uid обязан быть приведён к `str`: Telethon-путь уже даёт строку, но
    тесты и inline приходят с int.

    Отключение системной команды тут НЕ проверяется: выключенная
    команда не должна ни выполняться, ни подсказываться, но модуль,
    который её перехватил (решение «заменить»), — должен. Это решает
    вызывающий (`TelethonManager._handle_modules`).
    """
    owners = _index.get(str(head or "").strip().lower())
    if not owners:
        return None
    return owners.get(str(uid))


def find_inline(uid: str, key: str) -> InlineSpec | None:
    """Inline-обработчик по ключу для юзера."""
    target = str(key or "").strip().lower()
    if not target:
        return None
    for mod in list_modules(uid).values():
        for spec in mod.inline:
            if target == spec.key or target in spec.aliases:
                return spec
    return None


def _module_is_live(uid: str, name: str) -> bool:
    """Загружен ли модуль и не накопил ли он ошибку."""
    info = _registry.get(str(uid), {}).get(str(name))
    return info is not None and not info.error


# --------------------------------------------------------------------------
# Отключение системных команд
# --------------------------------------------------------------------------


def disabled_system(uid: str) -> set[str]:
    """Системные команды, выключенные юзером."""
    return set(_disabled_system.get(str(uid), set()))


def is_system_disabled(uid: str, head: str) -> bool:
    return str(head or "").strip().lower() in _disabled_system.get(str(uid), set())


def disable_system(uid: str, head: str) -> None:
    """Выключить системную команду для юзера.

    Команда не удаляется — она просто перестаёт вызываться, и если её
    перехватит модуль, юзер получит «свою» версию (см. `overrides_system`
    в `CommandSpec`).
    """
    _disabled_system.setdefault(str(uid), set()).add(str(head).strip().lower())


def enable_system(uid: str, head: str) -> None:
    _disabled_system.get(str(uid), set()).discard(str(head).strip().lower())


# --------------------------------------------------------------------------
# Загрузка / выгрузка
# --------------------------------------------------------------------------


def _purge_modules(module_id: str) -> None:
    """Убрать модуль и все его подмодули из `sys.modules`.

    Без этого `importlib` при перезаливке отдаёт объект из кеша, и новый
    код не применится (а `__pycache__` усугубляет).
    """
    for key in [k for k in sys.modules if k == module_id or k.startswith(module_id + ".")]:
        sys.modules.pop(key, None)
    try:
        importlib.invalidate_caches()
    except Exception:
        pass


def parse_module(path: Path) -> ModuleInfo:
    """Разобрать .py и вернуть описание модуля БЕЗ исполнения.

    Так мы ловим опечатки и опасные хвосты до того, как код юзера
    начнёт исполняться в процессе бота.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    limit = int(_cfg("MODULES_MAX_BYTES"))
    if len(raw.encode("utf-8")) > limit:
        raise ModuleError(f"Файл больше {limit} байт")

    try:
        tree = ast.parse(raw, filename=str(path))
    except SyntaxError as e:
        raise ModuleError(f"Синтаксис: строка {e.lineno}: {e.msg}") from e

    info = ModuleInfo(name=path.stem, user_id="", path=path)
    found_meta = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MODULE":
                    found_meta = True
                    info.title, info.version, info.author, info.description = _parse_meta(
                        node.value, path
                    )
    if not found_meta:
        raise ModuleError("Нет переменной MODULE = {...} — см. modules_dev.md")
    return info


def _parse_meta(node: ast.AST, path: Path) -> tuple[str, str, str, str]:
    """Достать поля из литерала `MODULE = {...}`.

    Только литералы: `eval` по произвольному выражению на этапе разбора
    исполнил бы код юзера до проверки прав.
    """
    if not isinstance(node, ast.Dict):
        raise ModuleError(f"MODULE в {path.name} должен быть словарём")
    out: dict[str, str] = {}
    for key, value in zip(node.keys, node.values):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, (str, int, float)):
            out[key.value] = str(value.value)
    title = out.get("name") or out.get("title") or path.stem
    return (
        title[:64],
        out.get("version", "0")[:16],
        out.get("author", "")[:64],
        out.get("description", "")[:400],
    )


def load_module(uid: str, name: str) -> ModuleInfo:
    """Загрузить (или перезагрузить) модуль юзера. Бросает `ModuleError`."""
    uid = str(uid)
    name = str(name)
    if not NAME_RE.match(name):
        raise ModuleError("Имя модуля: только латиница, цифры и _ (до 32 символов)")
    path = storage.module_dir(uid) / f"{name}.py"
    if not path.exists():
        raise ModuleError(f"Файл {name}.py не найден")

    info = parse_module(path)
    info.user_id = uid

    existing = _registry.get(uid, {}).get(name)
    if existing is not None:
        _unload_locked(uid, name)

    module_id = info.module_id
    _purge_modules(module_id)

    spec = importlib.util.spec_from_file_location(module_id, path)
    if spec is None or spec.loader is None:
        raise ModuleError("Не удалось создать спецификацию импорта")

    mod = importlib.util.module_from_spec(spec)
    # Регистрируем ДО exec: иначе `from <self> import ...` внутри модуля
    # не найдёт его, и в sys.modules не попадёт traceback.
    sys.modules[module_id] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        _purge_modules(module_id)
        raise ModuleError(f"{type(e).__name__}: {e}") from e

    info.router = getattr(mod, "router", None)
    info.loaded_at = time.time()
    _collect(mod, info)

    if not info.commands and not info.inline and info.router is None:
        _purge_modules(module_id)
        raise ModuleError(
            "Модуль ничего не зарегистрировал: нужна @command, @inline или router"
        )

    _registry.setdefault(uid, {})[name] = info
    _reindex()
    logger.info("modules: loaded %s for uid=%s (dot=%d inline=%d)",
                name, uid, len(info.commands), len(info.inline))
    return info


def _collect(mod: Any, info: ModuleInfo) -> None:
    """Собрать `@command`/`@inline`, объявленные в модуле.

    Декораторы кладут спеки в атрибуты функции (`_module_commands`),
    поэтому порядок объявления в файле не важен.
    """
    for attr in vars(mod).values():
        for spec in getattr(attr, "_module_commands", ()) or ():
            info.commands.append(spec)
        for spec in getattr(attr, "_module_inline", ()) or ():
            info.inline.append(spec)


def unload_module(uid: str, name: str) -> bool:
    """Выгрузить модуль. True, если он был загружен."""
    uid, name = str(uid), str(name)
    if name not in _registry.get(uid, {}):
        return False
    _unload_locked(uid, name)
    logger.info("modules: unloaded %s for uid=%s", name, uid)
    return True


def _unload_locked(uid: str, name: str) -> None:
    _unregister_rates(uid, name)
    info = _registry.get(uid, {}).pop(name, None)
    if info is not None:
        _purge_modules(info.module_id)
    if uid in _registry and not _registry[uid]:
        _registry.pop(uid, None)
    _reindex()


def reload_all() -> dict[str, str]:
    """Перезагрузить все модули всех юзеров. Возвращает {uid: ошибка|""}."""
    out: dict[str, str] = {}
    for uid, mods in list(_registry.items()):
        for name in list(mods):
            try:
                load_module(uid, name)
                out[uid] = ""
            except ModuleError as e:
                out[uid] = str(e)
    return out


def load_user_modules(uid: str) -> list[str]:
    """Загрузить все файлы юзера, включённые в storage. Возвращает ошибки."""
    errors: list[str] = []
    uid = str(uid)
    for name in list_files(uid):
        if not storage.is_module_enabled(uid, name):
            continue
        try:
            load_module(uid, name)
        except ModuleError as e:
            errors.append(f"{name}: {e}")
    return errors


def delete_module(uid: str, name: str) -> bool:
    """Выгрузить и стереть файл. True, если файл был."""
    uid, name = str(uid), str(name)
    unload_module(uid, name)
    storage.unregister_module(uid, name)
    path = storage.module_dir(uid) / f"{name}.py"
    if path.exists():
        path.unlink()
        return True
    return False


def save_module_source(uid: str, name: str, source: str) -> Path:
    """Записать исходник модуля на диск."""
    uid, name = str(uid), str(name)
    if not NAME_RE.match(name):
        raise ModuleError(
            f"Имя модуля {name!r}: только латиница, цифры и _ (до 32 символов)"
        )
    blob = source.encode("utf-8")
    limit = int(_cfg("MODULES_MAX_BYTES"))
    if len(blob) > limit:
        raise ModuleError(
            f"Файл {len(blob) // 1024} КБ, максимум {limit // 1024} КБ"
        )
    d = storage.module_dir(uid)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.py"
    tmp = path.with_suffix(".py.tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    return path


def check_install(uid: str, name: str, source: str) -> list[str]:
    """Проверить новый модуль до установки. Возвращает список проблем.

    Код НЕ исполняется: только `ast.parse` (проверка синтаксиса) и
    извлечение заявленных head'ов. Это важно — модуль может быть
    чужим, и до явного решения юзера он не должен начать работать.

    Проблемы делятся на два класса (см. `is_soft_conflict` в UI):
      * **конфликт** — команда занята (системная или чужая): модуль
        ставится, решение принимает юзер;
      * **ошибка** — модуль невалиден: не ставится вовсе.
    """
    uid, name = str(uid), str(name)
    problems: list[str] = []

    if not NAME_RE.match(name):
        problems.append(
            "Имя модуля: только латиница, цифры и _ (до 32 символов)"
        )

    try:
        ast.parse(source)
    except SyntaxError as e:
        return problems + [f"Синтаксис: строка {e.lineno}: {e.msg}"]

    heads, inline_keys = _declared_heads(source)
    if not heads and not inline_keys:
        return problems + ["Модуль не объявляет ни @command, ни @inline"]

    # Конфликт — только внутри пространства ЭТОГО юзера: у другого
    # юзера та же команда живёт в своём реестре и не мешает.
    for head in sorted(heads):
        if head in _system_heads:
            problems.append(f"{head} — системная команда")
        elif uid in _index.get(head, {}):
            problems.append(f"{head} — уже занят другим модулем")
    for key in sorted(inline_keys):
        if key in INLINE_SYSTEM_KEYS:
            problems.append(f"«{key}» — системный inline-запрос")
    return problems


def is_soft_conflict(problem: str) -> bool:
    """Мягкий конфликт (юзер решает) или жёсткая ошибка (не ставим)?"""
    return (
        "системная команда" in problem
        or "уже занят" in problem
        or "системный inline" in problem
    )


def _commands_of(uid: str) -> list[CommandSpec]:
    return [spec for mod in _registry.get(str(uid), {}).values() for spec in mod.commands]


#: Ключи, которые заняты встроенным inline (`handlers/inline/*`).
INLINE_SYSTEM_KEYS = frozenset({"помощь", "help", "справка", "h", "?", "статус", "status"})

def _declared_heads(source: str) -> tuple[set[str], set[str]]:
    """Извлечь head'ы команд и ключи inline из исходника, НЕ исполняя его.

    Идём по AST, а не по регулярке: `@command(".a", desc="x")` на
    нескольких строках, со скобками внутри строк и вложенными вызовами
    регулярку ломают, а `@command` и без `@` должен считаться
    (декоратор импортирован под другим именем — не наша забота, но
    хоть что-то находить полезно, чем молча пропускать).
    """
    heads: set[str] = set()
    keys: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return heads, keys

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else ""
        )
        if name not in ("command", "inline"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        value = first.value.strip().lower()
        if name == "command":
            head = value if value.startswith(".") else "." + value
            if CMD_RE.match(head):
                heads.add(head)
        else:
            key = value.lstrip("@").lstrip(".")
            if INLINE_RE.match(key):
                keys.add(key)
    return heads, keys


# --------------------------------------------------------------------------
# Индекс
# --------------------------------------------------------------------------


#: Бюджеты rate-limit, доступные модулям. Те же операции, что и у
#: встроенных команд (`utils/rate_limit_gate`), но модульные head'ы в
#: статический `utils/cmds.py` не попадают — он остаётся листовым.
MODULE_RATES = frozenset({"ai", "network", "heavy", "anim", "local"})


def _module_rate(spec: CommandSpec) -> str | None:
    """Бюджет, объявленный в `@command(rate=...)`."""
    value = getattr(spec.handler, "_module_rate", None)
    return str(value) if value in MODULE_RATES else None


def _register_rates(uid: str, mods: dict) -> None:
    """Прописать бюджеты модульных команд в гейт.

    `utils/rate_limit_gate.BUDGETS` — обычный словарь, поэтому
    модульные head'ы добавляются в него динамически и удаляются при
    выгрузке модуля. Без этого `rate=` в `@command` был бы
    декоративным: `gate.check` не нашёл бы бюджет и разрешил всё.
    """
    from utils import rate_limit_gate as gate
    from config import (
        AI_REQUEST_LIMIT,
        AI_REQUEST_WINDOW,
        EXPENSIVE_COMMAND_LIMIT,
        EXPENSIVE_COMMAND_WINDOW,
    )

    windows = {
        "ai": (AI_REQUEST_LIMIT, AI_REQUEST_WINDOW),
        "network": (EXPENSIVE_COMMAND_LIMIT, EXPENSIVE_COMMAND_WINDOW),
        "heavy": (EXPENSIVE_COMMAND_LIMIT, EXPENSIVE_COMMAND_WINDOW),
        "anim": (EXPENSIVE_COMMAND_LIMIT, EXPENSIVE_COMMAND_WINDOW),
        "local": (EXPENSIVE_COMMAND_LIMIT, EXPENSIVE_COMMAND_WINDOW),
    }
    for spec in (s for info in mods.values() if not info.error for s in info.commands):
        rate = _module_rate(spec)
        if rate is None:
            continue
        limit, window = windows[rate]
        for head in (spec.head, *spec.aliases):
            gate.BUDGETS[head.lower()] = (rate, limit, window)


def _unregister_rates(uid: str, name: str) -> None:
    """Снять бюджеты выгруженного модуля (иначе они бы «залипли»)."""
    from utils import rate_limit_gate as gate

    info = _registry.get(uid, {}).get(name)
    if info is None:
        return
    for spec in info.commands:
        for head in (spec.head, *spec.aliases):
            if gate.rate_limit_for(head) is not None:
                gate.BUDGETS.pop(head.lower(), None)


def _reindex() -> None:
    """Пересобрать `_index` и бюджеты rate-limit с нуля.

    Полная пересборка, а не точечное обновление: команды уезжают в
    `_renames`, и частичное обновление однажды оставило бы в индексе
    старый head. Индекс маленький (десятки записей на юзера), цена
    пересборки не имеет значения — событий с установкой модулей мало.
    """
    _reindex_plain()
    for uid, mods in _registry.items():
        _register_rates(uid, mods)
    _apply_renames()


#: Переименования команд: (uid, module, old_head) -> new_head.
_renames: dict[tuple[str, str, str], str] = {}


def override_head(uid: str, module: str, old_head: str, new_head: str) -> None:
    """Переименовать команду модуля (юзер выбрал «своё имя» в конфликте).

    Исходник не правится: он лежит на диске как прислали, и переписывать
    чужой файл — сюрприз для автора. Переименование живёт в реестре и
    применяется поверх после каждой загрузки модуля.
    """
    uid, module, old_head = str(uid), str(module), str(old_head).lower()
    head = _normalize_head(new_head)
    if not head:
        raise ModuleError(f"Некорректное имя команды: {new_head!r}")
    if head in _system_heads:
        raise ModuleError(f"{head} — системная команда")
    # Занято — только тем, что реально зарегистрировано **этим** юзером.
    # У другого юзера та же команда занимает своё пространство.
    if uid in _index.get(head, {}):
        raise ModuleError(f"{head} уже занят")
    _renames[(uid, module, old_head)] = head
    _reindex()


def rename_map(uid: str, module: str) -> dict[str, str]:
    """{old_head: new_head} для конкретного модуля."""
    return {
        old: new
        for (u, m, old), new in _renames.items()
        if u == str(uid) and m == str(module)
    }


def _apply_renames() -> None:
    """Проставить переименованные head'ы в индексе.

    Вызывается на каждой пересборке, потому что переименование переживает
    и перезагрузку модуля, и рестарт бота: оно лежит в реестре, а не в
    исходнике (см. `override_head`).
    """
    for (uid, module, old_head), new_head in list(_renames.items()):
        info = _registry.get(uid, {}).get(module)
        if info is None:
            continue
        for spec in info.commands:
            if old_head not in (spec.head, *spec.aliases):
                continue
            spec.head = new_head
            spec.aliases = tuple(
                new_head if h == old_head else h for h in spec.aliases
            )
            break
        _reindex_plain()


def _reindex_plain() -> None:
    """Пересобрать индекс из реестра, без применения переименований.

    Рекурсия с `_apply_renames` была бы бесконечной: точечное обновление
    индекса здесь всё равно перезаписывает переименованные head'ы, а они
    к этому моменту уже прописаны в самих спеках.
    """
    _index.clear()
    for uid, mods in _registry.items():
        for info in mods.values():
            if info.error:
                continue
            for spec in info.commands:
                for head in (spec.head, *spec.aliases):
                    _index.setdefault(head.lower(), {})[str(uid)] = spec


def reset_all() -> None:
    """Полный сброс реестра (используется в тестах)."""
    global _system_heads

    from utils import rate_limit_gate as gate

    for uid in list(_registry):
        for name in list(_registry[uid]):
            _unregister_rates(uid, name)
        for info in list(_registry[uid].values()):
            _purge_modules(info.module_id)
    # Модульные бюджеты могли остаться в гейте от не сброшенных head'ов.
    for head in [h for h in gate.BUDGETS if h not in _system_gate_snapshot()]:
        gate.BUDGETS.pop(head, None)
    _registry.clear()
    _index.clear()
    _disabled_system.clear()
    _renames.clear()
    _pending.clear()
    _system_heads = frozenset()


def _system_gate_snapshot() -> set[str]:
    """head'ы, зарегистрированные в гейте на момент импорта `cmds`."""
    from utils import cmds

    return {h.lower() for h in cmds.CMDS}


def command_heads(uid: str) -> list[str]:
    """Все head'ы модулей юзера (для подсказок и справки)."""
    return sorted({h for spec in _commands_of(uid) for h in (spec.head, *spec.aliases)})


# --------------------------------------------------------------------------
# Вызов
# --------------------------------------------------------------------------


def module_of_spec(uid: str, spec: CommandSpec) -> ModuleInfo | None:
    """Модуль, которому принадлежит спек команды. None — не нашёлся."""
    for info in _registry.get(str(uid), {}).values():
        for candidate in info.commands:
            if candidate is spec:
                return info
    return None


def module_of_inline(*, user_id: str, spec: InlineSpec) -> ModuleInfo | None:
    """Модуль, которому принадлежит спек inline-команды."""
    for info in _registry.get(str(user_id), {}).values():
        for candidate in info.inline:
            if candidate is spec:
                return info
    return None


async def run_inline(uid: str, spec: InlineSpec, ctx: Context) -> Any:
    """Вызвать inline-хендлер модуля под таймаутом.

    Возвращает то, что вернул хендлер (список aiogram-результатов либо
    `None`). Исключения ловятся и логируются: падение модуля не должно
    оставлять запрос без ответа — Telegram покажет «ничего не найдено»
    и повторит запрос сам.
    """
    if not module_allowed(uid):
        raise ModuleError("Модули запрещены для этого аккаунта (MODULE_ALLOWLIST)")
    try:
        timeout = int(_cfg("MODULES_COMMAND_TIMEOUT"))
        return await asyncio.wait_for(spec.handler(ctx), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("modules: inline %s timeout after %ss (uid=%s)",
                       spec.key, timeout, uid)
        return None
    except ModuleError:
        raise
    except Exception:
        logger.exception("modules: inline %s failed (uid=%s)", spec.key, uid)
        # Не возвращаем строку с ошибкой: результат inline-хендлера —
        # список aiogram-объектов, а строка привела бы к TypeError
        # на `inline.answer(results=...)`. Лучше тишина и запись в лог.
        return None


async def run_command(uid: str, spec: CommandSpec, ctx: Context) -> Any:
    """Вызвать хендлер модуля под таймаутом и с логированием ошибок.

    Ошибка модуля НЕ должна ронять бота и НЕ должна молчать: хендлер
    оборачивается, исключение ловится, юзеру уходит короткая карточка,
    подробности — в `bot.log`.
    """
    if not module_allowed(uid):
        raise ModuleError("Модули запрещены для этого аккаунта (MODULE_ALLOWLIST)")
    timeout = int(_cfg("MODULES_COMMAND_TIMEOUT"))
    try:
        return await asyncio.wait_for(spec.handler(ctx), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("modules: timeout %s after %ss (uid=%s)", spec.head, timeout, uid)
        return f"[x] Модуль не ответил за {timeout} с."
    except ModuleError:
        raise
    except Exception as e:
        logger.exception("modules: %s failed (uid=%s)", spec.head, uid)
        return f"[x] Ошибка модуля: {type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# Конфликты «модуль ↔ системная команда»
# --------------------------------------------------------------------------


@dataclass
class Conflict:
    """Конфликт между импортированной и системной командой."""

    uid: str
    module: str
    head: str
    system_title: str
    module_desc: str

    @property
    def key(self) -> str:
        """Ключ для callback_data (<= 64 байт, только [A-Za-z0-9_])."""
        h = hashlib.sha256(f"{self.uid}:{self.module}:{self.head}".encode()).hexdigest()
        return h[:16]


#: Отложенные решения по конфликтам: key -> Conflict.
_pending: dict[str, Conflict] = {}


def register_conflict(c: Conflict) -> None:
    _pending[c.key] = c


def pending_conflict(key: str) -> Conflict | None:
    return _pending.get(str(key))


def resolve_conflict(key: str, action: str, new_head: str = "") -> str:
    """Закрыть конфликт. `action`: keep | replace | rename | rollback.

    Возвращает короткий код результата для UI.
    """
    c = _pending.pop(str(key), None)
    if c is None:
        return "expired"
    if action == "keep":
        enable_system(c.uid, c.head)
        return "kept"
    if action == "replace":
        disable_system(c.uid, c.head)
        return "replaced"
    if action == "rename":
        head = _normalize_head(new_head)
        if not head:
            return "bad_name"
        if head in _system_heads or head in _index:
            return "taken"
        return f"renamed:{head}"
    if action == "rollback":
        return "rolled_back"
    return "unknown"


def _normalize_head(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if not value.startswith("."):
        value = "." + value
    return value if CMD_RE.match(value) else ""


def system_command_title(head: str) -> str:
    """Человеческое имя системной команды — из реестра `_helpdb`."""
    try:
        from handlers.commands._helpdb import ALIASES, HELPS

        key = ALIASES.get(str(head).lstrip(".").lower())
        if key and key in HELPS:
            return key
    except Exception:
        pass
    return str(head)


def inline_bot_at() -> str:
    return bot_username_at()


__all__ = [
    "CMD_RE",
    "CommandSpec",
    "Conflict",
    "Context",
    "INLINE_RE",
    "INLINE_SYSTEM_KEYS",
    "InlineSpec",
    "MODULE_PREFIX",
    "ModuleError",
    "ModuleInfo",
    "NAME_RE",
    "allowlist_active",
    "command_heads",
    "delete_module",
    "disable_system",
    "disabled_system",
    "enable_system",
    "find_command",
    "find_inline",
    "installed_names",
    "is_system_disabled",
    "list_files",
    "list_modules",
    "load_module",
    "load_user_modules",
    "module_allowed",
    "modules_enabled",
    "pending_conflict",
    "register_conflict",
    "reload_all",
    "resolve_conflict",
    "run_command",
    "save_module_source",
    "set_system_heads",
    "system_command_title",
    "system_heads",
    "unload_module",
]