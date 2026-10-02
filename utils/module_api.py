"""Публичный API для авторов модулей.

Модуль импортирует **только** этот модуль:

    from slimbot_api import Module, command, inline, on_message

Полное описание — `modules_dev.md`.

`Module` — фабрика декларации: держит собранные спеки, чтобы
`@command` мог положить их рядом с функцией (декоратор не знает
владельца). Декларация на уровне модуля, а не экземпляра: созданный
объект `mod` можно не использовать вообще, регистрация идёт при
импорте.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from utils.modules import CommandSpec, Context, InlineSpec

__all__ = [
    "CommandSpec",
    "Context",
    "InlineSpec",
    "Module",
    "command",
    "inline",
    "on_message",
]


class Module:
    """Контейнер деклараций одного модуля.

    Необязателен: `@command`/`@inline` работают и без него. Полезен,
    чтобы задать общие для всех команд хендлера поля (rate, paths).
    """

    def __init__(
        self,
        *,
        rate: str | None = None,
        paths: tuple[str, ...] = ("telethon", "bot"),
    ) -> None:
        self.rate = rate
        self.paths = tuple(paths)
        self.commands: list[CommandSpec] = []
        self.inline: list[InlineSpec] = []

    def __repr__(self) -> str:  # pragma: no cover - только для логов
        return f"<Module commands={len(self.commands)} inline={len(self.inline)}>"


def _norm_heads(*raw: str) -> tuple[str, ...]:
    """Нормализовать и провалидировать head'ы команд.

    head обязан начинаться с точки и не содержать пробелов: диспетчер
    берёт первый токен строки, `. foo` и `foo` не совпали бы никогда.
    """
    from utils.modules import CMD_RE

    out: list[str] = []
    for item in raw:
        for part in str(item).split(","):
            head = part.strip().lower()
            if not head:
                continue
            if not head.startswith("."):
                head = "." + head
            if not CMD_RE.match(head):
                raise ValueError(
                    f"Некорректная команда {part!r}: ожидается .имя "
                    "(точка, без пробелов, до 32 символов)"
                )
            if head not in out:
                out.append(head)
    if not out:
        raise ValueError("@command: укажи хотя бы одну команду, например @command('.note')")
    return tuple(out)


def _norm_keys(*raw: str) -> tuple[str, ...]:
    from utils.modules import INLINE_RE

    out: list[str] = []
    for item in raw:
        for part in str(item).split(","):
            key = part.strip().lower().lstrip("@").lstrip(".")
            if not key:
                continue
            if not INLINE_RE.match(key):
                raise ValueError(f"Некорректный inline-ключ {part!r}")
            if key not in out:
                out.append(key)
    if not out:
        raise ValueError("@inline: укажи хотя бы один ключ, например @inline('заметка')")
    return tuple(out)


def command(
    *cmds: str,
    aliases: tuple[str, ...] = (),
    desc: str = "",
    key: str = "",
    rate: str | None = None,
    paths: tuple[str, ...] = ("telethon", "bot"),
    overrides: str = "",
    hides: str = "",
) -> Callable[[Callable[[Context], Awaitable[Any]]], Callable[[Context], Awaitable[Any]]]:
    """Зарегистрировать dot-команду модуля.

    Args:
        *cmds: команда и её алиасы, с точкой или без: `".note"`, `"note"`.
            Хотя бы одна обязательна.
        aliases: дополнительные алиасы (удобно, если они не в первом
            аргументе — читаемее в коде модуля).
        desc: текст справки, показывается в `.help` и в списке модулей.
        key: ключ для `.команда справка`. По умолчанию — имя команды без
            точки; задаётся явно, если совпадает с системной.
        rate: бюджет rate-limit (`"network"`, `"heavy"`, `"local"`, `"ai"`,
            `"anim"` или `None`). `None` — без ограничения.
        paths: `("telethon",)` — только в чатах, `("bot",)` — только в
            личке с ботом, оба — везде (по умолчанию).
        overrides: head системной команды, которую модуль заменяет.
        hides: head системной команды, которую модуль отключает для себя.

    Returns:
        Декоратор: возвращает функцию без изменений, спек ложится в
        атрибут `_module_commands`.

    Raises:
        ValueError: если нет ни одной команды или head некорректен.
    """
    heads = _norm_heads(*cmds, *aliases)

    def decorator(fn: Callable[[Context], Awaitable[Any]]):
        spec = CommandSpec(
            head=heads[0],
            aliases=tuple(heads[1:]),
            handler=fn,
            desc=desc,
            key=key or heads[0].lstrip(".").replace(".", "_"),
            overrides_system=_norm_one(overrides),
            hides=_norm_one(hides),
        )
        declared = list(getattr(fn, "_module_commands", []) or [])
        declared.append(spec)
        fn._module_commands = declared  # type: ignore[attr-defined]
        fn._module_rate = rate          # type: ignore[attr-defined]
        fn._module_paths = tuple(paths)  # type: ignore[attr-defined]
        return fn

    return decorator


def _norm_one(raw: str) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    return value if value.startswith(".") else "." + value


def inline(
    *keys: str,
    aliases: tuple[str, ...] = (),
    desc: str = "",
) -> Callable[[Callable[[Context], Awaitable[Any]]], Callable[[Context], Awaitable[Any]]]:
    """Зарегистрировать inline-команду: `@bot <key> …`.

    Хендлер получает `Context`, где `ctx.args` — остаток запроса после
    ключа, и возвращает список aiogram-объектов
    `InlineQueryResultArticle` / `InlineQueryResultPhoto` / … либо `None`.

    Returns:
        Декоратор; спек попадает в атрибут `_module_inline`.
    """
    norm = _norm_keys(*keys, *aliases)

    def decorator(fn: Callable[[Context], Awaitable[Any]]):
        spec = InlineSpec(
            key=norm[0],
            aliases=tuple(norm[1:]),
            handler=fn,
            desc=desc,
        )
        declared = list(getattr(fn, "_module_inline", []) or [])
        declared.append(spec)
        fn._module_inline = declared  # type: ignore[attr-defined]
        return fn

    return decorator


def on_message(router: Any) -> Callable[[Callable], Callable]:
    """Декоратор aiogram-хендлера модуля.

    Модуль может отдать свой `router = Router()` и повесить на него
    обычные aiogram-хендлеры — они будут работать в личке с ботом.
    Создавать надо **новый** `Router`, а не брать `handlers.commands.router`:
    на родительском роутере любой message-хендлер блокирует `sub_routers`
    (см. AGENTS.md, инвариант 1).

    Args:
        router: экземпляр `aiogram.Router`.

    Returns:
        Декоратор, регистрирующий функцию как message-хендлер.
    """
    def decorator(fn: Callable):
        router.message.register(fn)
        return fn

    return decorator