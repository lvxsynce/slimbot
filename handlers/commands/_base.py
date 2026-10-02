from datetime import datetime, timezone
import re

from utils.bot_info import bot_username_at
from utils.timezones import format_with_tz
from utils.texts import Texts, render_for_user


#: Telegram отклоняет сообщения длиннее 4096 символов, а ошибка отправки
#: проглатывалась вызывающим кодом — пользователь просто видел команду без
#: ответа. Здесь режем заранее, сохраняя валидный HTML.
TELEGRAM_MAX_MESSAGE = 4096
#: Запас на суффикс, который дописывается при обрезке.
TRUNCATE_HEADROOM = 64
_TRUNCATE_HEADROOM = TRUNCATE_HEADROOM


#: Теги, которые мы умеем закрывать при обрезке.
_CLOSABLE = ("code", "b", "i", "s", "u", "a", "blockquote")


def _utf16_len(text: str) -> int:
    """Длина строки в UTF-16 code units — ровно то, что считает Telegram.

    ``len()`` даёт code points, а для emoji (astral, 2 code units каждый)
    расходится вдвое: карточка на 3071 code points = 6072 UTF-16 units,
    и Telegram её отвергнет.
    """
    return len(text.encode("utf-16-le", errors="replace")) // 2


def _truncate(text: str, limit: int = TELEGRAM_MAX_MESSAGE) -> str:
    """Обрезать до лимита Telegram по UTF-16, не разрывая HTML-теги.

    Режем по code point'ам, но проверяем результат по UTF-16 — и продолжаем
    резать, пока не влезем. Плюс баланс тегов: незакрытый ``<code>`` делает
    карточку невалидной, и Telegram вернёт BadRequest — пользователь снова
    ничего не увидит.
    """
    if _utf16_len(text) <= limit:
        return text
    # Режем бинарным поиском по code point'ам: '_utf16_len' монотонна.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _utf16_len(text[:mid]) <= limit - _TRUNCATE_HEADROOM:
            lo = mid
        else:
            hi = mid - 1
    clipped = text[:lo]

    # Отрезаем хвост, если он попал внутрь открывающего тега.
    last_lt = clipped.rfind("<")
    if last_lt != -1 and ">" not in clipped[last_lt:]:
        clipped = clipped[:last_lt]

    for tag in _CLOSABLE:
        opens = len(re.findall(rf"<{tag}(?:\s[^>]*)?>", clipped))
        closes = len(re.findall(rf"</{tag}>", clipped))
        clipped += f"</{tag}>" * max(0, opens - closes)

    return clipped.rstrip() + "\n<i>…обрезано.</i>"


_LEGACY_HEADER_RE = re.compile(r"^<b>Slim bot \| [^<]+</b>\s*")
_WRAPPED_QUOTE_RE = re.compile(r"<blockquote(?:\s+[^>]*)?>([\s\S]*)</blockquote>")


def _unwrap_legacy(body: str) -> str:
    """Снять legacy-обёртку: старые хендлеры уже собирали карточку сами.

    Некоторые клали header внутрь quote, другие — снаружи. Снимаем и то и
    то, пока не останется голое тело. Раньше этот цикл был продублирован
    дважды подряд в одной функции, причём вторая копия была мёртвой.
    """
    previous = None
    while previous != body:
        previous = body
        body = _LEGACY_HEADER_RE.sub("", body).strip()
        match = _WRAPPED_QUOTE_RE.fullmatch(body)
        if match:
            body = match.group(1).strip()
    return body


def command_card(title: str, body: str, *, expandable: bool = False) -> str:
    """Build the single HTML layout used by command responses."""
    title = str(title).strip()
    body = _unwrap_legacy(str(body or "").strip())
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    card = f"<b>Slim bot | {title}</b>\n{tag}{body}</blockquote>"
    return _truncate(card, TELEGRAM_MAX_MESSAGE)


def normalize_command_card(text: str, title: str = "Result") -> str:
    """Normalize legacy command HTML at the final aiogram send boundary."""
    return command_card(title, str(text or "").strip())


def command_response(title: str, text: str) -> str:
    """Return a command response with the canonical Slim bot header."""
    return normalize_command_card(text, title)




async def render_time(user_id, *, tz_label: str | None = None) -> str:
    """``.time`` — рендер с TZ + premium-aware эмодзи.

    Карточка целиком (header + ``<blockquote>``) собирается здесь, чтобы
    был ровно ОДИН blockquote. Раньше ``Texts.Time.TIME`` оборачивал уже
    готовую карточку из ``format_with_tz`` — получался вложенный blockquote,
    который Telegram отклоняет.
    """
    from utils.storage import get_user_tz
    label = tz_label if tz_label is not None else (get_user_tz(user_id) if user_id is not None else None)
    now_str, _ = format_with_tz(label)
    body = await render_for_user(user_id, Texts.Time.TIME, now=now_str)
    return command_card("Time", body)


async def render_id(owner_id, *, chat_id, user_id, username, first_name, thread_id=None) -> str:
    """``.id`` — рендер с premium-aware эмодзи + escape пользовательских данных."""
    from utils.escape import esc
    topic_line = f"\n• Topic ID: <code>{int(thread_id)}</code>" if thread_id else ""
    return await render_for_user(
        owner_id,
        Texts.ID.INFO,
        chat_id=str(chat_id),
        topic_line=topic_line,
        user_id=str(user_id),
        username=esc(username or "нет"),
        name=esc(first_name or ""),
    )


def thread_kwargs(message) -> dict:
    """Параметры для aiogram send-методов, чтобы ответ шёл в тот же топик форума."""
    tid = getattr(message, "message_thread_id", None)
    if not tid:
        return {}
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return {}
    if tid <= 0:
        return {}
    return {"message_thread_id": tid}


async def reply_in_topic(message, text, **kwargs):
    """message.reply с авто-пробросом message_thread_id для форумов."""
    kw = thread_kwargs(message)
    kw.update(kwargs)
    return await message.reply(text, **kw)


async def answer_in_topic(message, text, **kwargs):
    """message.answer с авто-пробросом message_thread_id для форумов."""
    kw = thread_kwargs(message)
    kw.update(kwargs)
    return await message.answer(text, **kw)


async def dispatch(message, text: str, **kwargs):
    """Единая точка отправки ответа в aiogram handlers (личка с ботом)."""
    title = kwargs.pop("card_title", None)
    if title:
        text = command_card(title, text)
    try:
        return await message.reply(text, **thread_kwargs(message), **kwargs)
    except Exception as e:
        # MessageNotModified в некоторых aiogram версиях бросает BadRequest,
        # не отдельный класс. Проверяем по тексту.
        if "not modified" in str(e).lower():
            return
        raise


def _help_base_lines() -> list[str]:
    """Базовые команды — работают всегда (в личке с ботом)."""
    return [
        "• <code>.ping</code> / <code>.пинг</code> — задержка",
        "• <code>.time</code> / <code>.время</code> — время в твоей таймзоне",
        "• <code>.id</code> / <code>.инфо</code> — ID чата",
        "• <code>.me</code> / <code>.я</code> — мой профиль",
        "• <code>.chat</code> / <code>.чат</code> — о чате",
        "• <code>.who</code> / <code>.кто</code> — об отправителе (reply)",
        "• <code>.net</code> — анализ IP, домена или URL",
        "• <code>.hash</code> / <code>.хеш</code> — md5/sha1/sha256... (или файл по reply)",
        "• <code>.uuid</code> / <code>.юид</code> — сгенерить UUID4 (до 20 штук)",
        "• <code>.b64</code> / <code>.base64</code> — base64 encode/decode",
        "• <code>.timezone</code> / <code>.tz</code> — твоя таймзона для <code>.time</code>",
        "• <code>.tr</code> / <code>.перевод</code> — перевод",
        "• <code>.calc</code> / <code>.калк</code> — калькулятор",
        "• <code>.love</code> / <code>.любовь</code> — сердечко",
        "• <code>.монетка</code> / <code>.coin</code> — орёл или решка",
        "• <code>.ии</code> &lt;запрос&gt; — запрос к ИИ",
        "• <code>.watch</code> / <code>.следить</code> — отслеживать чат",
        "• <code>.unwatch</code> / <code>.хватит</code> — перестать",
        "• <code>.watched</code> / <code>.список</code> — список",
        "• <code>.help</code> / <code>.помощь</code> — это сообщение",
    ]


def _help_session_lines() -> list[str]:
    """Команды Telethon-сессии — работают в любых чатах при активной сессии."""
    return [
        "• <code>.del [N]</code> / <code>.удалить</code> — снести N своих сообщений",
        "• <code>.save</code> / <code>.сохранить</code> — в Избранное (reply)",
        "• <code>.pin</code> / <code>.закрепить</code> — закрепить сообщение",
        "• <code>.unpin</code> / <code>.открепить</code> — снять закрепление",
        "• <code>.admins</code> / <code>.админы</code> — список админов чата",
        "• <code>.tagall</code> / <code>.все</code> — тегнуть всех в группе",
        "• <code>.влс</code> [текст] — в личку автору (reply)",
        "• <code>.watch @username</code> — добавить по юзернейму",
        "• <code>.watch -100123</code> — по ID",
        "• <code>.ссылка</code> / <code>.invitelink</code> — инвайт-ссылка",
        "• Команды работают в <b>любых чатах</b>",
    ]


def _help_inline_lines() -> list[str]:
    """Inline-команды — работают в любом чате, где юзер набирает @username."""
    b = bot_username_at()
    return [
        f"• <code>{b} помощь</code> — справка по inline",
        f"• <code>{b} статус</code> — аптайм, пинг, нагрузка",
        f"• <code>{b} @username</code> — карточка профиля (нужна сессия)",
    ]


def _help_lines(has_session: bool) -> list[str]:
    """Единый текст справки: полный список всегда, состояние сессии
    меняет только заголовок раздела и хинт внизу."""
    lines = _help_base_lines()
    lines += [
        "",
        "<b>Inline</b> — в любом чате (кнопка «Включить inline» в <code>/start</code>):",
        *_help_inline_lines(),
    ]
    lines += [
        "",
        "<b>Дополнительно:</b>" if has_session else "<b>Дополнительно (нужна сессия):</b>",
        *_help_session_lines(),
        "",
        "<i>Подсказка:</i> <code>.команда справка</code> — детали по любой команде.",
    ]
    if has_session:
        lines += [
            "",
            "[i] Отключить сессию: <b>/logout</b>.",
        ]
    else:
        lines += [
            "",
            "[i] Подключи <b>дополнительные возможности</b> (кнопка ниже) — сессионные команды заработают в любых чатах.",
        ]
    return lines


def format_help(has_session: bool = False) -> str:
    """``.help`` — скрытая справка по командам.

    Структура такая же, как раньше. Для premium используй :func:`render_help`.
    """
    title = Texts.Help.TITLE.render(premium=False)
    base = ["<blockquote expandable>", title, "", *_help_lines(has_session), "</blockquote>"]
    return command_card("Help", "\n".join(base))


async def render_help(uid, has_session: bool = False) -> str:
    """``.help`` — финальный HTML со скрытым expandable-блоком."""
    title = await render_for_user(uid, Texts.Help.TITLE)
    base = ["<blockquote expandable>", title, "", *_help_lines(has_session), "</blockquote>"]
    return command_card("Help", "\n".join(base))
