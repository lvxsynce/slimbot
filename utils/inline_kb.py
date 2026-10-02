"""Клавиатуры inline-режима.

**Почему отдельный модуль.** Bot API не умеет ставить placeholder в поле
ввода: единственный механизм «подсказки» — inline-кнопка
`switch_inline_query_chosen_chat` / `..._current_chat`. Нажатие переводит
юзера в inline-режим в выбранном чате и подставляет `@<bot> <query>` в
поле ввода. Обе клавиатуры нужны в двух местах — над списком inline-
результатов (`handlers/inline/*`) и в личке с ботом (`handlers/commands/
start.py`, `help.py`), — поэтому модуль вынесен в `utils/` и остаётся
**листом**: тянет только `aiogram.types`, не импортирует telethon и не
создаёт цикла с `handlers.inline`.
"""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultsButton,
    SwitchInlineQueryChosenChat,
)

#: `start_parameter` в deep-link на /start: принимает только
#: `[A-Za-z0-9_-]{1,64}`.
START_PARAMETER = "start"

DEFAULT_SWITCH_TEXT = "[?] Включить inline в любом чате"


def connect_button(
    text: str = "[+] Подключить бота",
    parameter: str = START_PARAMETER,
) -> InlineQueryResultsButton:
    """Кнопка над списком inline-результатов: уводит в личку на /start."""
    return InlineQueryResultsButton(text=text, start_parameter=parameter)


def switch_inline_markup(
    query: str = "",
    *,
    text: str = DEFAULT_SWITCH_TEXT,
    allow_user_chats: bool = True,
    allow_bot_chats: bool = True,
    allow_group_chats: bool = True,
    allow_channel_chats: bool = True,
) -> InlineKeyboardMarkup:
    """Клавиатура-подсказка: по нажатию юзер выбирает чат, и inline-режим
    открывается в нём с уже подставленным ``@<bot> <query>``.

    ``chosen_chat``, а не ``current_chat``: `/start` и `/help` приходят в
    личку с ботом, и `current_chat` открыл бы inline в диалоге с самим
    ботом — то есть ровно там, где он бесполезен.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text,
                    switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
                        query=query or None,
                        allow_user_chats=allow_user_chats,
                        allow_bot_chats=allow_bot_chats,
                        allow_group_chats=allow_group_chats,
                        allow_channel_chats=allow_channel_chats,
                    ),
                ),
            ],
        ],
    )


def with_inline_hint(kbd: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Дописывает строку с inline-подсказкой в конец существующей клавиатуры.

    Нужен там, где клавиатура уже собрана из callback-кнопок (`/start`,
    `/help`): вызывающий код не должен знать про устройство подсказки.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[*kbd.inline_keyboard, *switch_inline_markup().inline_keyboard],
    )