"""Централизованная локализация всех текстов бота + поддержка премиум-эмодзи.

Каждый текст представлен объектом :class:`Text`:
- ``template``  — шаблон с плейсхолдерами ``{имя}``. Имена из ``emojis``
  автоматически заменяются на эмодзи (premium-aware). Остальные имена
  берутся из ``**values`` в :meth:`Text.render`.
- ``emojis``    — словарь ``{плейсхолдер → Emoji}``. У одного ``Text``
  может быть несколько разных emoji-плейсхолдеров; один и тот же эмодзи
  можно использовать в нескольких местах шаблона.

Класс :class:`Emoji`:
- ``fallback``   — обычный unicode-эмодзи, видимый всем.
- ``premium_id`` — Telegram custom emoji ID (опционально). Если задан И у
  отправителя есть Telegram Premium, в HTML выводится
  ``<tg-emoji emoji-id="...">fallback</tg-emoji>``; иначе просто ``fallback``.
- ``comment``    — где именно в тексте этот эмодзи вставлен (человеческое
  описание для документации и code review).

Премиум-эмодзи работают ТОЛЬКО если у отправителя есть Telegram Premium:
- В Telethon — у самого пользователя (``client.get_me().premium``).
- В aiogram через обычный чат с ботом — бот не может иметь Premium,
  поэтому эмодзи ВСЕГДА отрисуются как fallback (юзер увидит unicode).

Если ``premium_id`` не задан (None), эмодзи ВСЕГДА отрисуется как fallback —
это поведение по умолчанию, пока вы не подставите реальный custom emoji ID.

Как использовать в handler'е::

    from utils.texts import Texts, render_for_user
    text = await render_for_user(uid, Texts.Ping.PING, ms="23")
    await message.edit_text(text)

Или для случая, когда у нас уже есть Telethon entity (без лишнего I/O)::

    from utils.texts import render_for_entity
    text = render_for_entity(sender_entity, Texts.Me.CARD, ...)

Каждое определение ``Text`` задокументировано комментарием с указанием,
где используется и какие значения нужно передать в ``render()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Optional


# ============================================================
# Core dataclasses
# ============================================================


@dataclass(frozen=True)
class Emoji:
    """Один эмодзи с опциональной премиум-версией.

    Attributes:
        fallback:   Обычный unicode-эмодзи, видимый всем.
        premium_id: ID Telegram custom emoji для премиум-отображения.
                    ``None`` → fallback всегда (нет анимированной версии).
        comment:    Где в тексте этот эмодзи вставлен (для code review).
    """

    fallback: str
    premium_id: Optional[str] = None
    comment: str = ""

    def render(self, premium: bool) -> str:
        """Рендерит эмодзи с учётом премиум-статуса отправителя.

        Telegram рендерит ``<tg-emoji>`` анимированным только если у
        SENDER'а есть Premium; иначе fallback unicode. На стороне
        бота мы не можем это контролировать — поэтому всегда отдаём
        тег, а Telegram сам решает как отрисовать.
        """
        if self.premium_id and premium:
            return f'<tg-emoji emoji-id="{self.premium_id}">{self.fallback}</tg-emoji>'
        return self.fallback


@dataclass(frozen=True)
class Text:
    """Шаблон текста с плейсхолдерами эмодзи и значений.

    В ``template`` используются ``{имя}``:
    - Имя из ``emojis`` → автозамена на :meth:`Emoji.render` (premium-aware).
    - Любое другое имя → автозамена через ``**values`` в :meth:`render`.

    Плейсхолдеры эмодзи подставляются ДО значений — поэтому конфликта
    имён между ними не возникает (значение с тем же именем, что у эмодзи,
    будет перезаписано; для эмодзи передавайте через ``emojis``).

    Если в шаблоне есть плейсхолдер из ``values``, которого нет в
    ``emojis`` — он остаётся как ``{name}`` в выводе. Это by design:
    ``str.replace`` тихо игнорирует отсутствующие ключи, и если в
    ``values`` есть лишний ключ — он просто не используется.
    """

    template: str
    emojis: dict[str, Emoji] = field(default_factory=dict)
    comment: str = ""

    def render(self, *, premium: bool = False, **values: Any) -> str:
        """Подставляет эмодзи (premium-aware) + значения из ``values``.

        Args:
            premium: True если у отправителя есть Telegram Premium.
            **values: Значения для не-emoji плейсхолдеров.

        Returns:
            Готовый HTML для отправки (parse_mode=HTML/Html).
        """
        out = self.template
        # 1) Эмодзи — первыми (их имена могут пересечься с values,
        #    но мы хотим, чтобы emoji-логика была приоритетнее).
        for name, em in self.emojis.items():
            token = "{" + name + "}"
            if token in out:
                out = out.replace(token, em.render(premium))
        # 2) Обычные значения.
        for k, v in values.items():
            out = out.replace("{" + k + "}", str(v))
        out = re.sub(r"(<b>)\s+", r"\1", out)
        out = re.sub(r"\s+(</b>)", r"\1", out)
        match = re.fullmatch(
            r"\s*<blockquote(?:\s+[^>]*)?>\s*(<b>.*?</b>)\s*([\s\S]*?)\s*</blockquote>\s*",
            out,
        )
        if match:
            title, body = match.groups()
            body = body.strip()
            if title.startswith("<b>Slim bot | "):
                return f"{title}\n<blockquote>{body}</blockquote>" if body else title
            command = re.search(r"(?:\.|/)([a-zа-я]+)", self.comment.lower())
            label = command.group(1).capitalize() if command else "Result"
            header = f"<b>Slim bot | {label}</b>"
            inner = title + (f"\n{body}" if body else "")
            return f"{header}\n<blockquote>{inner}</blockquote>"
        bold_quote = re.fullmatch(
            r"\s*<blockquote(?:\s+[^>]*)?>\s*(<b>.*?</b>)\s*</blockquote>\s*",
            out,
        )
        if bold_quote:
            command = re.search(r"\.([a-zа-я]+)", self.comment.lower())
            label = command.group(1).capitalize() if command else "Result"
            return f"<b>Slim bot | {label}</b>\n<blockquote>{bold_quote.group(1)}</blockquote>"
        plain_quote = re.fullmatch(
            r"\s*<blockquote(?:\s+[^>]*)?>\s*([\s\S]*?)\s*</blockquote>\s*",
            out,
        )
        if plain_quote:
            command = re.search(r"\.([a-zа-я]+)", self.comment.lower())
            label = command.group(1).capitalize() if command else "Result"
            return f"<b>Slim bot | {label}</b>\n<blockquote>{plain_quote.group(1).strip()}</blockquote>"
        return out


# ============================================================
# Render helpers
# ============================================================


async def render_for_user(user_id: int | str | None, text: Text, **values: Any) -> str:
    """Рендер ``Text`` с проверкой Premium через Telethon-сессию юзера.

    Подходит для:
    - aiogram handlers: ``user_id`` = ``message.from_user.id`` в личке с ботом;
    - Telethon handlers: ``user_id`` = ID текущего юзера.

    Если ``user_id`` is None → premium=False (fallback на unicode).
    """
    if user_id is None:
        return text.render(premium=False, **values)
    # Lazy import — premium.py импортирует telethon_manager, тот — bot, и т.д.
    from utils.premium import is_user_premium

    premium = await is_user_premium(user_id)
    return text.render(premium=premium, **values)


def render_for_entity(entity: Any, text: Text, **values: Any) -> str:
    """Рендер ``Text`` на основе уже-резолвленного User entity.

    Работает с обоими типами:
    - aiogram ``types.User``: ``.is_premium``
    - Telethon ``User``:      ``.premium``

    Никаких I/O — для случаев, когда entity уже есть (Telethon
    ``event.get_sender()``, ``get_entity()`` и т.п.).
    """
    premium = bool(
        getattr(entity, "premium", getattr(entity, "is_premium", False))
    )
    return text.render(premium=premium, **values)


def render_plain(text: Text, **values: Any) -> str:
    """Рендер без премиум-эмодзи (всегда fallback).

    Использовать:
    - для inline-кнопок и callback-ответов (там premium-эмодзи не нужны);
    - для сообщений, где у нас НЕТ user_id (например, bot-initiated
      уведомления об удалении/подключении);
    - как fallback при ошибках определения premium.
    """
    return text.render(premium=False, **values)


# ============================================================
# Texts — namespace-классы для группировки
# ============================================================


class Texts:
    """Все тексты бота. Группируются по командам через внутренние классы.

    Конвенция:
    - ``Texts.<Команда>.<ИМЯ_КОНСТАНТЫ>`` — объект ``Text``.
    - Имя константы = UPPER_SNAKE_CASE, отражает содержимое.
    - Для команд с несколькими вариантами вывода — несколько констант.
    """

    # ----------------------------------------------------------------
    # .ping
    # ----------------------------------------------------------------
    class Ping:
        """``.ping`` / ``.пинг`` — задержка между отправкой и обработкой."""

        PING = Text(
            template=(
                "<b>Slim bot | Ping</b>\n"
                "<blockquote>\n"
                "Telegram API RTT: <code>{ms}ms</code>\n"
                "</blockquote>"
            ),
            emojis={
                "pong": Emoji(
                    fallback="🏓",
                    premium_id=None,
                    comment="заголовок 'Pong!'",
                ),
            },
            comment=".ping — основной ответ. values: ms=str(ping_ms)",
        )

    # ----------------------------------------------------------------
    # .time
    # ----------------------------------------------------------------
    class Time:
        """``.time`` / ``.время`` — текущее время в таймзоне юзера."""

        TIME = Text(
            template=(
                "<blockquote>"
                "{clock} <b>Текущее время</b>\n"
                "{now}"
                "</blockquote>"
            ),
            emojis={
                "clock": Emoji(
                    fallback="⏰",
                    premium_id=None,
                    comment="заголовок 'Текущее время'",
                ),
            },
            comment=".time — основной ответ. values: now=str(formatted_time)",
        )

    # ----------------------------------------------------------------
    # .id / .инфо
    # ----------------------------------------------------------------
    class ID:
        """``.id`` / ``.инфо`` — ID чата + юзера + топика + username."""

        INFO = Text(
            template=(
                "<blockquote>"
                "<b>{info} Информация:</b>\n"
                "• Chat ID: <code>{chat_id}</code>{topic_line}\n"
                "• User ID: <code>{user_id}</code>\n"
                "• Username: @{username}\n"
                "• Имя: {name}"
                "</blockquote>"
            ),
            emojis={
                "info": Emoji(
                    fallback="ℹ️",
                    premium_id=None,
                    comment="заголовок 'Информация:'",
                ),
            },
            comment=".id — карточка чата/юзера. values: chat_id, user_id, "
            "topic_line (пустая строка или '\\n• Topic ID: <code>N</code>'), "
            "username, name",
        )

    # ----------------------------------------------------------------
    # .love / .govno
    # ----------------------------------------------------------------
    class Love:
        """``.love`` / ``.любовь`` + ``.govno`` / ``.говно`` — анимированные ASCII-картинки.

        Обе команды используют один и тот же движок анимации (3 цикла по 7
        emoji, шаг 0.45с). Финал разный: LOVE_YOU — для ``.love``, GOVNO_DONE —
        для ``.govno``.
        """

        # Внешний «холодный» цвет (бьющиеся шаги) — 7 цветов.
        # У этих эмодзи нет premium-версий; premium-логика не нужна.
        FRAME_EAGLE = Text(
            template="{emoji}",
            emojis={
                "emoji": Emoji(
                    fallback="❤️",
                    premium_id=None,
                    comment="центральный символ в кадре сердечка",
                ),
            },
            comment=".love — один кадр сердечка. values: (нет)",
        )

        # Финальная подпись после анимации.
        LOVE_YOU = Text(
            template="{heart} <b>Love you</b> {heart}",
            emojis={
                "heart": Emoji(
                    fallback="❤️",
                    premium_id=None,
                    comment="обрамляет 'Love you' слева и справа",
                ),
            },
            comment=".love — финальный кадр. values: (нет)",
        )

        # Финальная подпись после анимации .govno.
        GOVNO_DONE = Text(
            template="{poop} <b>Какашечка</b> {poop}",
            emojis={
                "poop": Emoji(
                    fallback="💩",
                    premium_id=None,
                    comment="обрамляет 'Какашечка' слева и справа",
                ),
            },
            comment=".govno — финальный кадр. values: (нет)",
        )

    # ----------------------------------------------------------------
    # .coin / .монетка
    # ----------------------------------------------------------------
    class Coin:
        """``.монетка`` / ``.coin`` / ``.орёл`` / ``.решка`` — подбросить монетку."""

        HEAD = Text(
            template="<blockquote><b>{eagle} Орёл</b></blockquote>",
            emojis={
                "eagle": Emoji(
                    fallback="🦅",
                    premium_id=None,
                    comment="слева от слова 'Орёл'",
                ),
            },
            comment=".coin — орёл выпал. values: (нет)",
        )

        TAIL = Text(
            template="<blockquote><b>{coin} Решка</b></blockquote>",
            emojis={
                "coin": Emoji(
                    fallback="🪙",
                    premium_id=None,
                    comment="слева от слова 'Решка'",
                ),
            },
            comment=".coin — решка выпала. values: (нет)",
        )

    # ----------------------------------------------------------------
    # .me / .я
    # ----------------------------------------------------------------
    class Me:
        """``.me`` / ``.я`` — карточка своего профиля."""

        TITLE = Text(
            template="<b>{user} Я</b>",
            emojis={
                "user": Emoji(
                    fallback="👤",
                    premium_id=None,
                    comment="заголовок 'Я'",
                ),
            },
            comment=".me — заголовок карточки. values: (нет)",
        )

        PREMIUM_YES = Text(
            template="⭐ Premium: да",
            comment=".me — индикатор Premium для самого юзера (НЕ премиум-эмодзи; "
            "обычный звёздочный fallback)",
        )

    # ----------------------------------------------------------------
    # .chat / .чат
    # ----------------------------------------------------------------
    class Chat:
        """``.chat`` / ``.чат`` — инфо о текущем чате."""

        TITLE = Text(
            template="<b>{bubble} Чат</b>",
            emojis={
                "bubble": Emoji(
                    fallback="💬",
                    premium_id=None,
                    comment="заголовок 'Чат'",
                ),
            },
            comment=".chat — заголовок. values: (нет)",
        )

    # ----------------------------------------------------------------
    # .who / .кто
    # ----------------------------------------------------------------
    class Who:
        """``.who`` / ``.кто`` — карточка отправителя replied-сообщения."""

        TITLE = Text(
            template="<b>{lens} Кто это</b>",
            emojis={
                "lens": Emoji(
                    fallback="🔍",
                    premium_id=None,
                    comment="заголовок 'Кто это'",
                ),
            },
            comment=".who — заголовок. values: (нет)",
        )

        NO_REPLY = Text(
            template="<blockquote>[x] Ответь этой командой на сообщение.</blockquote>",
            comment=".who — нет reply. values: (нет)",
        )

        NO_SENDER = Text(
            template="<blockquote>[x] Не удалось определить отправителя.</blockquote>",
            comment=".who — не определён отправитель. values: (нет)",
        )

    # ----------------------------------------------------------------
    # .watch / .unwatch / .watched
    # ----------------------------------------------------------------
    class Watch:
        """``.watch`` / ``.unwatch`` / ``.watched`` — отслеживание чатов."""

        WATCH_OK = Text(
            template="[OK] <b>{label}</b> — теперь отслеживается.{photo_note}",
            comment=".watch — успешно добавлено. values: label, photo_note",
        )

        WATCH_ALREADY = Text(
            template="[!] <b>{label}</b> — уже в списке.",
            comment=".watch — уже было. values: label",
        )

        WATCH_RESOLVED_OK = Text(
            template=(
                "[OK] <b>{title}</b> (<code>{chat_id}</code>) — "
                "теперь отслеживается (весь чат).{photo_note}"
            ),
            comment=".watch @user — резолв по юзернейму/ID. "
            "values: title, chat_id, photo_note",
        )

        WATCH_NOT_FOUND = Text(
            template="[x] Не удалось найти чат: <code>{args}</code>",
            comment=".watch @bad — резолв не нашёл. values: args",
        )

        WATCH_NEED_SESSION = Text(
            template=(
                "[x] <code>.watch @username</code> и <code>.watch -100...</code> "
                "требуют Telethon-сессию.\n\n"
                "[i] Нажми /start и подключи «дополнительные возможности», "
                "чтобы это заработало.\n\n"
                "Без сессии работает только <code>.watch</code> в чате, "
                "где находишься."
            ),
            comment=".watch @user в личке без сессии — нужно подключить Telethon.",
        )

        UNWATCH_OK = Text(
            template="[-] <b>{label}</b> — отслеживание отключено.",
            comment=".unwatch — успешно. values: label",
        )

        UNWATCH_NOT = Text(
            template="[!] <b>{label}</b> — не был в списке.",
            comment=".unwatch — не было. values: label",
        )

        WATCHED_HEADER = Text(
            template="<b>Отслеживаемые чаты:</b>",
            comment=".watched — заголовок списка. values: (нет)",
        )

        WATCHED_EMPTY = Text(
            template="[.] Нет отслеживаемых чатов.",
            comment=".watched — пусто. values: (нет)",
        )

        WATCHED_HINT = Text(
            template=(
                "[.] Нет отслеживаемых чатов.\n\n"
                "Введи <code>.следить</code> в нужном чате, чтобы начать.\n\n"
                "<b>С Telethon-сессией ещё:</b>\n"
                "• <code>.watch @username</code> — добавить по юзернейму\n"
                "• <code>.watch -100123</code> — добавить по ID"
            ),
            comment=".watched (aiogram) — пусто + подсказка.",
        )

        PHOTO_NOTE_ON = Text(
            template="\n\n[i] Одноразовые фото из этого чата будут сохраняться.",
            comment="суффикс к WATCH_OK: фото будут сохраняться (есть сессия).",
        )

        PHOTO_NOTE_OFF = Text(
            template="\n\n[i] Одноразовые фото не сохраняются — нужна сессия.",
            comment="суффикс к WATCH_OK: фото НЕ сохраняются (нет сессии).",
        )

    class Hash:
        """``.hash`` / ``.хеш`` — хеш текста/файла."""

        HELP = Text(
            template=(
                "<blockquote>[?] Использование:\n"
                "<code>.hash sha256 текст</code> — хеш текста\n"
                "<code>.hash sha256</code> (reply) — хеш текста реплая\n"
                "<code>.hash sha256</code> (reply на файл/фото/видео/аудио) — хеш файла\n"
                "Алгоритмы: {algos}.</blockquote>"
            ),
            comment=".hash — usage. values: algos",
        )

        TITLE = Text(
            template="<b>{lock} {algo}</b>",
            emojis={
                "lock": Emoji(
                    fallback="🔐",
                    premium_id=None,
                    comment="заголовок 'алгоритм'",
                ),
            },
            comment=".hash — заголовок. values: algo",
        )

        NO_TEXT = Text(
            template="<blockquote>[x] Нет текста для хеширования.</blockquote>",
            comment=".hash — нечего хешировать.",
        )

        DOWNLOAD_FAIL = Text(
            template=(
                "<blockquote>[x] Не удалось скачать <code>{hint}</code> "
                "(слишком большой или недоступен).</blockquote>"
            ),
            comment=".hash — не удалось скачать reply-файл. values: hint",
        )

    class Uuid:
        """``.uuid`` / ``.юид`` — UUIDv4 генератор."""

        TITLE = Text(
            template="<b>{tag} UUID4 ×{n}</b>",
            emojis={
                "tag": Emoji(
                    fallback="🆔",
                    premium_id=None,
                    comment="заголовок 'UUID4 ×N'",
                ),
            },
            comment=".uuid — заголовок. values: n",
        )

    class B64:
        """``.b64`` / ``.base64`` — base64 encode/decode."""

        HELP = Text(
            template=(
                "<blockquote>[?] Использование:\n"
                "<code>.b64 текст</code> — encode\n"
                "<code>.b64 decode текст</code> — decode\n"
                "<code>.b64 url …</code> — url-safe вариант (-_ вместо +/)\n"
                "<code>.b64</code> (reply) — encode текста</blockquote>"
            ),
            comment=".b64 — usage.",
        )

        TITLE = Text(
            template="<b>{lock} b64 {mode}</b>",
            emojis={
                "lock": Emoji(
                    fallback="🔐",
                    premium_id=None,
                    comment="заголовок 'b64 MODE'",
                ),
            },
            comment=".b64 — заголовок. values: mode (encode/decode/url-encode/url-decode)",
        )

        EMPTY = Text(
            template="<blockquote>[x] Нечего кодировать.</blockquote>",
            comment=".b64 — пустой ввод.",
        )

    # ----------------------------------------------------------------
    # .tr / .calc / .save
    # ----------------------------------------------------------------
    class Tr:
        """``.tr`` / ``.перевод`` — перевод."""

        HELP = Text(
            template=(
                "<blockquote>[?] Использование:\n"
                "<code>.tr en привет</code>\n"
                "<code>.tr en</code> (ответом на сообщение)\n"
                "<code>.tr</code> (по умолчанию ru)</blockquote>"
            ),
            comment=".tr — usage.",
        )

        @staticmethod
        def format(src: str, tgt: str, lang: str, detected: str) -> str:
            """Перевод в HTML. Не premium-aware — текст результата варьируется."""
            if detected and detected != lang:
                head = f"<b>{detected} → {lang}</b>"
            else:
                head = f"<b>→ {lang}</b>"
            return f"<blockquote>{head}\n{tgt}</blockquote>"

    class Calc:
        """``.calc`` / ``.калк`` — калькулятор."""

        HELP = Text(
            template="<blockquote>[?] <code>.calc 2 + 2 * 3</code></blockquote>",
            comment=".calc — usage.",
        )

        RESULT = Text(
            template="<blockquote><code>{expr}</code>\n= <b>{res}</b></blockquote>",
            comment=".calc — результат. values: expr, res",
        )

    class Save:
        """``.save`` / ``.сохранить`` — в Избранное (нужна Telethon-сессия)."""

        NEED_SESSION = Text(
            template=(
                "<blockquote>[x] <code>.save</code> требует Telethon-сессию.\n"
                "Нажми /start → «Включить».</blockquote>"
            ),
            comment=".save — без сессии (в личке).",
        )

        NEED_SESSION_PRIVATE = Text(
            template=(
                "<blockquote>[x] <code>.save</code> требует Telethon-сессию.</blockquote>"
            ),
            comment=".save — без сессии (в личке).",
        )

        NEED_REPLY = Text(
            template="<blockquote>[?] Ответь этой командой на сообщение, которое надо сохранить.</blockquote>",
            comment=".save — без reply.",
        )

        NEED_REPLY_SHORT = Text(
            template="<blockquote>[?] Ответь этой командой на сообщение.</blockquote>",
            comment=".save — без reply (короткий).",
        )

        NO_SESSION = Text(
            template="<blockquote>[x] Сессия не активна.</blockquote>",
            comment=".save — сессия пропала между проверками.",
        )

        OK = Text(
            template="<blockquote>[OK] Сохранено в Избранное.</blockquote>",
            comment=".save — успех.",
        )

    # ----------------------------------------------------------------
    # .timezone
    # ----------------------------------------------------------------
    class Timezone:
        """``.timezone`` / ``.таймзона`` / ``.tz`` — настройка TZ."""

        TITLE = Text(
            template="<b>{globe} Часовая зона</b>",
            emojis={
                "globe": Emoji(
                    fallback="🌍",
                    premium_id=None,
                    comment="заголовок 'Часовая зона'",
                ),
            },
            comment=".timezone — заголовок списка. values: (нет)",
        )

        CURRENT = Text(
            template="Сейчас: <code>{cur}</code>",
            comment=".timezone (без аргумента) — текущее значение. values: cur",
        )

        CURRENT_DEFAULT = Text(
            template="Сейчас: <code>UTC</code> (по умолчанию)",
            comment=".timezone (без аргумента) — TZ не задана.",
        )

        EXAMPLES = Text(
            template="<b>Примеры:</b>",
            comment=".timezone — подзаголовок 'Примеры'.",
        )

        EXAMPLE_LINES = (
            "• <code>.timezone +3</code> — Москва, СПб",
            "• <code>.timezone -5</code> — Нью-Йорк",
            "• <code>.timezone +5:30</code> — Индия",
            "• <code>.timezone Europe/Moscow</code> — по имени",
            "• <code>.timezone МСК</code> / <code>.timezone киев</code> — алиас",
            "• <code>.timezone UTC</code> / <code>.timezone reset</code> — сброс",
        )

        PRESETS_TITLE = Text(
            template="<b>Пресеты:</b>",
            comment=".timezone — подзаголовок 'Пресеты'.",
        )

        APPLY_HINT = Text(
            template="[i] Применяется к <code>.time</code>.",
            comment=".timezone — хинт в конце списка.",
        )

        RESET_OK = Text(
            template="<blockquote>[OK] TZ: <code>UTC</code> (по умолчанию)</blockquote>",
            comment=".timezone reset — успех.",
        )

        BAD = Text(
            template=(
                "<blockquote>[x] Не знаю таймзону: <code>{args}</code>\n"
                "Примеры: <code>.timezone +3</code>, "
                "<code>.timezone Europe/Moscow</code>, "
                "<code>.timezone МСК</code>.\n"
                "Без аргумента — список пресетов.</blockquote>"
            ),
            comment=".timezone — невалидный ввод. values: args",
        )

        SET_OFFSET = Text(
            template="<blockquote>[OK] TZ: <code>{label}</code></blockquote>",
            comment=".timezone — установка offset. values: label (UTC+3 etc)",
        )

        SET_ALIAS = Text(
            template="<blockquote>[OK] TZ: <code>{canonical}</code> ({alias})</blockquote>",
            comment=".timezone — установка по алиасу. values: canonical, alias",
        )

        SET_IANA = Text(
            template="<blockquote>[OK] TZ: <code>{canonical}</code></blockquote>",
            comment=".timezone — установка IANA. values: canonical",
        )

    # ----------------------------------------------------------------
    # .quote
    # ----------------------------------------------------------------
    class Quote:
        """``.quote`` / ``.цитата`` — Telethon-only. Reply на сообщение."""

        NEED_REPLY = Text(
            template=(
                "<blockquote>[?] Ответь этой командой на сообщение, "
                "которое хочешь оформить как цитату.</blockquote>"
            ),
            comment=".quote — нет reply.",
        )

        NO_BODY = Text(
            template=(
                "<blockquote>[?] Сообщение без текста (медиа-only). "
                "Цитировать нечего.</blockquote>"
            ),
            comment=".quote — reply без текста/caption.",
        )

        TITLE = Text(
            template="<b>{speech} Цитата</b> · <i>{date}</i>",
            emojis={
                "speech": Emoji(
                    fallback="💬",
                    premium_id=None,
                    comment="заголовок 'Цитата'",
                ),
            },
            comment=".quote — заголовок. values: date",
        )

        FWD_LINE = Text(
            template="\n<i>↪ переслано от: {fwd}</i>",
            comment=".quote — forward-info строка. values: fwd (esc)",
        )

        FWD_LINE_ID = Text(
            template="\n<i>↪ переслано от: <code>{fwd_id}</code></i>",
            comment=".quote — forward-info по ID. values: fwd_id",
        )

        SIGNATURE = Text(
            template="<i>— {sender_link}{chat_line}</i>",
            comment=".quote — подпись внизу. values: sender_link, chat_line",
        )

    # ----------------------------------------------------------------
    # .cmd справка / .cmd help
    # ----------------------------------------------------------------
    class Cmdhelp:
        """``.cmd справка`` / ``.cmd help`` — per-command help."""

        TITLE = Text(
            template="<b>{book} {syntax}</b>",
            emojis={
                "book": Emoji(
                    fallback="📖",
                    premium_id=None,
                    comment="заголовок карточки help",
                ),
            },
            comment=".cmd справка — заголовок. values: syntax",
        )

        NOT_FOUND = Text(
            template="<blockquote>[x] Нет справки по <code>{key}</code></blockquote>",
            comment=".cmd справка — нет такого ключа. values: key",
        )

    # ----------------------------------------------------------------
    # /help, .help, .помощь — полная справка
    # ----------------------------------------------------------------
    class Help:
        """``.help`` / ``.помощь`` / ``/help`` — список всех команд."""

        TITLE = Text(
            template="<b>{book} Команды</b>",
            emojis={
                "book": Emoji(
                    fallback="📚",
                    premium_id=None,
                    comment="заголовок справки",
                ),
            },
            comment=".help — заголовок.",
        )

        TIP = Text(
            template=(
                "<i>Подсказка:</i> <code>.команда справка</code> — "
                "детали по любой команде."
            ),
            comment=".help — подсказка про `.cmd справка`.",
        )

        WITH_SESSION_HEADER = Text(
            template="<b>С сессией ещё:</b>",
            comment=".help — заголовок секции с сессией.",
        )

        NO_SESSION_HINT = Text(
            template=(
                "[i] Подключи <b>дополнительные возможности</b> (кнопка ниже) — "
                "появятся <code>.del</code>, <code>.save</code>, "
                "<code>.watch @user</code>, и команды будут работать везде."
            ),
            comment=".help — хинт без сессии.",
        )

        DISABLE_HINT = Text(
            template="[i] Отключить сессию: <b>/logout</b>.",
            comment=".help — хинт в самом конце.",
        )

    # ----------------------------------------------------------------
    # /start
    # ----------------------------------------------------------------
    class Start:
        """``/start`` — статус подключения + кнопки."""

        CONNECTED_GREETING = Text(
            template=(
                "Привет! Я <b>{bot_name}</b> (@{username}).\n\n"
                "Telethon-сессия: {ss_status}\n\n"
                "<b>Команды:</b>\n"
                "• <code>.watch</code> / <code>.следить</code> — отслеживать чат\n"
                "• <code>.unwatch</code> / <code>.хватит</code> — перестать\n"
                "• <code>.watched</code> / <code>.список</code> — список чатов\n"
                "• <code>.help</code> — все команды\n\n"
                "Отключить сессию: <b>/logout</b>."
            ),
            comment="/start (connected). values: bot_name, username, ss_status",
        )

        DISCONNECTED_GREETING = Text(
            template=(
                "Привет! Я <b>{bot_name}</b> (@{username}).\n\n"
                "Нажми <b>[+] Включить</b> ниже, чтобы подключить Telethon-сессию.\n\n"
                "После подключения команды будут работать в любых чатах."
            ),
            comment="/start (not connected). values: bot_name, username",
        )

        SS_ON = Text(
            template="✅ Включено",
            comment="/start — индикатор сессии (включена).",
        )

        SS_OFF = Text(
            template="❌ Выключено",
            comment="/start — индикатор сессии (выключена).",
        )

    # ----------------------------------------------------------------
    # /status
    # ----------------------------------------------------------------
    class Status:
        """``/status`` — статус Telethon-сессии."""

        CONNECTED = Text(
            template="Сессия активна, команды работают в любых чатах.",
            comment="/status (подключён).",
        )

        DISCONNECTED = Text(
            template="Сессия не подключена. Нажми /start → [+] Включить.",
            comment="/status (не подключён).",
        )

    # ----------------------------------------------------------------
    # /logout
    # ----------------------------------------------------------------
    class Logout:
        """``/logout`` — отзыв Telethon-сессии."""

        CONFIRM = Text(
            template=(
                "<b>Выключить дополнительные возможности?</b>\n\n"
                "Сессия будет отозвана в Telegram, файл сессии — удалён.\n"
                "После этого <code>.watch @username</code> и сохранение "
                "одноразовых фото перестанут работать."
            ),
            comment="/logout — подтверждение.",
        )

        NOT_NEEDED = Text(
            template="[i] Дополнительные возможности не подключены — нечего выключать.",
            comment="/logout — нет сессии.",
        )

        CANCELLED = Text(
            template="[i] Отменено.",
            comment="/logout — отмена.",
        )

        ALREADY_OFF = Text(
            template="[i] Сессия уже не активна.",
            comment="/logout — сессия уже отключена.",
        )

        DONE = Text(
            template="[OK] Выключено.\n{note}",
            comment="/logout — успех. values: note",
        )

        NOTE_REVOKED = Text(
            template="Сессия отозвана в Telegram.",
            comment="/logout — успех (отозвана).",
        )

        NOTE_LOCAL = Text(
            template="Сессия удалена локально.",
            comment="/logout — успех (только локально).",
        )

    # ----------------------------------------------------------------
    # .ai / .ии (статические плейсхолдеры; динамика — LLM)
    # ----------------------------------------------------------------
    class Ai:
        """``.ии`` / ``.ai`` — ИИ-ассистент. Только статические UI-строки."""

        THINKING = Text(
            template="[…] Думаю…",
            comment=".ии — placeholder во время запроса к LLM.",
        )

        THINKING_ICON = Text(
            template="{brain} Думаю…",
            emojis={
                "brain": Emoji(
                    fallback="🧠",
                    premium_id=None,
                    comment="иконка перед 'Думаю…'",
                ),
            },
            comment=".ии — thinking с эмодзи.",
        )

        TITLE = Text(
            template="<b>{robot} ИИ</b>",
            emojis={
                "robot": Emoji(
                    fallback="🤖",
                    premium_id=None,
                    comment="заголовок 'ИИ' в footer'е ответа",
                ),
            },
            comment=".ии — заголовок финального ответа. values: (нет)",
        )

        RESET_ONE = Text(
            template="[OK] История диалога сброшена (<code>{n}</code> сообщений).",
            comment=".ии сброс — очистка одного чата. values: n",
        )

        RESET_ALL = Text(
            template=(
                "[OK] Сброшена история во всех чатах "
                "(<code>{n}</code> диалогов)."
            ),
            comment=".ии сброс все — очистка всех чатов. values: n",
        )

    # ----------------------------------------------------------------
    # .del / .tagall / .влс / .admins / .pin / .invitelink
    # ----------------------------------------------------------------
    class Delmsg:
        """``.del`` / ``.удалить`` — Telethon-only."""

        USAGE = Text(
            template="<blockquote>[x] Использование: <code>.del N</code></blockquote>",
            comment=".del — usage.",
        )

        NO_PERMS = Text(
            template="<blockquote>[x] Нет прав на удаление.</blockquote>",
            comment=".del — MessageDeleteForbiddenError.",
        )

        FLOOD = Text(
            template="<blockquote>[x] FloodWait {seconds}s</blockquote>",
            comment=".del — FloodWait. values: seconds",
        )

        ERR = Text(
            template="<blockquote>[x] {etype}: {msg}</blockquote>",
            comment=".del — generic exception. values: etype, msg",
        )

    class Tagall:
        """``.tagall`` / ``.все`` — Telethon-only."""

        ONLY_GROUPS = Text(
            template="<blockquote>[x] Работает только в группах.</blockquote>",
            comment=".tagall — не группа.",
        )

        NEED_ADMIN = Text(
            template="<blockquote>[x] Нужны права администратора.</blockquote>",
            comment=".tagall — нет прав.",
        )

        ERR = Text(
            template="<blockquote>[x] {etype}: {msg}</blockquote>",
            comment=".tagall — generic exception. values: etype, msg",
        )

        NO_PARTICIPANTS = Text(
            template="<blockquote>[.] Нет участников.</blockquote>",
            comment=".tagall — пустой список.",
        )

        NO_USERS = Text(
            template="<blockquote>[.] Нет пользователей для тега.</blockquote>",
            comment=".tagall — отфильтровали всех.",
        )

    class Dm:
        """``.влс`` / ``.лс`` / ``.dm`` — Telethon-only."""

        NEED_REPLY = Text(
            template="<blockquote>[?] Ответь этой командой на сообщение.</blockquote>",
            comment=".влс — без reply.",
        )

    # ----------------------------------------------------------------
    # .nya (catgirl rewrite исходящих сообщений юзера в чате) — Telethon-only
    # ----------------------------------------------------------------
    class Nya:
        """``.ня`` — включить/выключить режим catgirl-rewrite в текущем чате.

        Фича Telethon-only (aiogram НЕ МОЖЕТ перехватить исходящие сообщения
        юзера). Хранится в utils/storage.py::nya_chats.
        """

        ON_OK = Text(
            template=(
                "<blockquote>[OK] {paw} Режим <b>ня</b> ВКЛЮЧЁН в этом чате.\n"
                "Все твои сообщения будут отредактированы в стиле кошко-девочки.\n\n"
                "<i>Выключить:</i> <code>.ня стоп</code> · <code>.ня выкл</code>\n"
                "<i>Список активных чатов:</i> <code>.ня список</code></blockquote>"
            ),
            emojis={
                "paw": Emoji(
                    fallback="🐾",
                    premium_id=None,
                    comment="слева от 'Режим ня ВКЛЮЧЁН'",
                ),
            },
            comment=".ня — успешно включено. values: (нет)",
        )

        ON_ALREADY = Text(
            template="<blockquote>[i] {paw} <b>ня</b> уже включён в этом чате.</blockquote>",
            emojis={
                "paw": Emoji(
                    fallback="🐾",
                    premium_id=None,
                    comment="слева от 'ня уже включён'",
                ),
            },
            comment=".ня — уже включён (toggle). values: (нет)",
        )

        OFF_OK = Text(
            template="<blockquote>[OK] {paw} Режим <b>ня</b> выключен в этом чате.</blockquote>",
            emojis={
                "paw": Emoji(
                    fallback="🐾",
                    premium_id=None,
                    comment="слева от 'Режим ня выключен'",
                ),
            },
            comment=".ня стоп/выкл — успех. values: (нет)",
        )

        OFF_NOT = Text(
            template="<blockquote>[i] {paw} <b>ня</b> не был включён в этом чате.</blockquote>",
            emojis={
                "paw": Emoji(
                    fallback="🐾",
                    premium_id=None,
                    comment="слева от 'ня не был включён'",
                ),
            },
            comment=".ня стоп/выкл — не был включён. values: (нет)",
        )

        PRIVATE = Text(
            template="<blockquote>[x] <b>ня</b> работает только в чатах (не в личке с ботом).</blockquote>",
            comment=".ня в личке с ботом — нечего редактировать вне твоего аккаунта.",
        )

        NEED_SESSION = Text(
            template=(
                "<blockquote>[x] <b>ня</b> требует Telethon-сессию.\n\n"
                "Нажми /start → «Включить» для подключения.</blockquote>"
            ),
            comment=".ня — нет Telethon-сессии (aiogram-сценарий).",
        )

        LIST_EMPTY = Text(
            template="<blockquote>[i] {paw} Нет активных чатов с режимом <b>ня</b>.</blockquote>",
            emojis={
                "paw": Emoji(
                    fallback="🐾",
                    premium_id=None,
                    comment="слева от 'Нет активных чатов'",
                ),
            },
            comment=".ня список — пусто.",
        )

        LIST_HEADER = Text(
            template="<b>{paw} Чаты с режимом ня:</b>",
            emojis={
                "paw": Emoji(
                    fallback="🐾",
                    premium_id=None,
                    comment="заголовок списка",
                ),
            },
            comment=".ня список — заголовок.",
        )

    class Admins:
        """``.admins`` / ``.админы`` — Telethon-only."""

        ONLY_GROUPS = Text(
            template="<blockquote>[x] Работает только в группах/каналах.</blockquote>",
            comment=".admins — не группа.",
        )

        NEED_ADMIN = Text(
            template="<blockquote>[x] Нужны права администратора.</blockquote>",
            comment=".admins — нет прав.",
        )

        ERR = Text(
            template="<blockquote>[x] {etype}: {msg}</blockquote>",
            comment=".admins — generic exception. values: etype, msg",
        )

        TITLE = Text(
            template="<b>{crown} Администраторы ({n})</b>",
            emojis={
                "crown": Emoji(
                    fallback="👑",
                    premium_id=None,
                    comment="заголовок 'Администраторы (N)'",
                ),
            },
            comment=".admins — заголовок. values: n",
        )

        EMPTY = Text(
            template="<blockquote>[.] Нет администраторов.</blockquote>",
            comment=".admins — пусто.",
        )

    class Pin:
        """``.pin`` / ``.закрепить`` / ``.unpin`` / ``.открепить`` — Telethon-only."""

        NEED_ADMIN = Text(
            template="<blockquote>[x] Нужны права администратора для закрепления.</blockquote>",
            comment=".pin — нет прав.",
        )

        DISABLED = Text(
            template="<blockquote>[x] В этом чате закрепление запрещено.</blockquote>",
            comment=".pin — chat pin disabled.",
        )

        PINNING = Text(
            template="<blockquote>[…] Закрепляю…</blockquote>",
            comment=".pin — progress.",
        )

        UNPINNING_ALL = Text(
            template="<blockquote>[…] Снимаю все закрепления…</blockquote>",
            comment=".unpin all — progress.",
        )

        UNPINNING_ONE = Text(
            template="<blockquote>[…] Открепляю {label}…</blockquote>",
            comment=".unpin (one) — progress. values: label",
        )

        NEED_ADMIN_UNPIN = Text(
            template="<blockquote>[x] Нужны права администратора.</blockquote>",
            comment=".unpin — нет прав.",
        )

        DONE_ALL_UNPIN = Text(
            template="<blockquote>[OK] {pin} Все закрепления сняты.</blockquote>",
            emojis={
                "pin": Emoji(
                    fallback="📌",
                    premium_id=None,
                    comment="префикс '[OK]' в строке 'Все закрепления сняты'",
                ),
            },
            comment=".unpin all — успех.",
        )

        DONE_ONE_UNPIN = Text(
            template="<blockquote>[OK] {pin} {label} откреплено.</blockquote>",
            emojis={
                "pin": Emoji(
                    fallback="📌",
                    premium_id=None,
                    comment="префикс '[OK]' в строке '<label> откреплено'",
                ),
            },
            comment=".unpin (one) — успех. values: label",
        )

        ERR = Text(
            template="<blockquote>[x] {etype}: {msg}</blockquote>",
            comment=".pin/.unpin — generic exception. values: etype, msg",
        )

    class Invitelink:
        """``.ссылка`` / ``.invitelink`` / ``.инвайт`` — Telethon-only."""

        NEED_ADMIN = Text(
            template="<blockquote>[x] Нужны права администратора.</blockquote>",
            comment=".ссылка — нет прав.",
        )

        GETTING = Text(
            template="<blockquote>[…] Получаю ссылку…</blockquote>",
            comment=".ссылка — progress.",
        )

        ERR = Text(
            template="<blockquote>[x] {etype}: {msg}</blockquote>",
            comment=".ссылка — generic exception. values: etype, msg",
        )

    # ----------------------------------------------------------------
    # Telethon-only: dot-команды в любых чатах (multi-target .who, etc.)
    # ----------------------------------------------------------------
    class Telethon:
        """Dot-команды Telethon-слоя. Каждая константа = один Text."""

        # Multi-target .who progress
        WHO_PROGRESS = Text(
            template="[…] Резолвлю {n} цель(ей)…",
            comment="Telethon .who @a @b — progress. values: n",
        )

        # Final summary for multi-target .who
        WHO_SUMMARY_OK = Text(
            template="[OK] {ok} resolved из {total}",
            comment=".who multi — все ОК. values: ok, total",
        )

        WHO_SUMMARY_FAIL = Text(
            template="[!] {ok} resolved, {fail} failed:\n{fails}",
            comment=".who multi — есть failures. values: ok, fail, fails",
        )

    # ----------------------------------------------------------------
    # Inline handlers
    # ----------------------------------------------------------------
    class Inline:
        """Inline-кнопки и кнопки ``[Подключить бота]``."""

        CONNECT_BTN = Text(
            template="[+] Подключить бота",
            comment="InlineQueryResultsButton (start_parameter='start').",
        )

    # ----------------------------------------------------------------
    # Common / shared
    # ----------------------------------------------------------------
    class Common:
        """Переиспользуемые куски."""

        # Кнопка /help + /status reply_markup hint
        FOOTER_HINT = Text(
            template="[i] Отключить сессию: <b>/logout</b>.",
            comment="footer — хинт в нескольких ответах.",
        )


__all__ = ["Emoji", "Text", "Texts", "render_for_user", "render_for_entity", "render_plain"]
