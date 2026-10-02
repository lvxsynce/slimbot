"""Публичная точка входа для модулей.

Модуль пишет:

    from slimbot_api import command, inline

и больше ничего. Имя файла в корне (а не в `utils/`) сделано специально:
автору не нужно знать внутреннюю раскладку проекта, а при переезоне
меняется одна строка в этом файле.
"""

from utils.module_api import (
    CommandSpec,
    Context,
    InlineSpec,
    Module,
    command,
    inline,
    on_message,
)

__all__ = [
    "CommandSpec",
    "Context",
    "InlineSpec",
    "Module",
    "command",
    "inline",
    "on_message",
]