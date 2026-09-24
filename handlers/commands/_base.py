from datetime import datetime, timezone
import re

from utils.timezones import format_with_tz
from utils.texts import Texts, render_for_user


def command_card(title: str, body: str, *, expandable: bool = False) -> str:
    """Build the single HTML layout used by command responses."""
    title = str(title).strip()
    body = str(body or "").strip()
    body = re.sub(r"^<b>Slim bot \| [^<]+</b>\s*", "", body)
    while True:
        match = re.fullmatch(r"<blockquote(?:\s+[^>]*)?>([\s\S]*)</blockquote>", body)
        if not match:
            break
        body = match.group(1).strip()
    # A few older handlers put the Slim bot header inside the quote. Remove it
    # before adding the canonical header outside the quote.
    body = re.sub(r"^<b>Slim bot \| [^<]+</b>\s*", "", body)
    while True:
        match = re.fullmatch(r"<blockquote(?:\s+[^>]*)?>([\s\S]*)</blockquote>", body)
        if not match:
            break
        body = match.group(1).strip()
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"<b>Slim bot | {title}</b>\n{tag}{body}</blockquote>"


def normalize_command_card(text: str, title: str = "Result") -> str:
    """Normalize legacy command HTML at the final aiogram send boundary."""
    return command_card(title, str(text or "").strip())


def command_response(title: str, text: str) -> str:
    """Return a command response with the canonical Slim bot header."""
    return normalize_command_card(text, title)


def format_ping(message_date) -> str:
    """``.ping`` — задержка между отправкой и обработкой.

    Возвращает готовый HTML с unicode-эмодзи (НЕ premium-aware). Для premium
    используй :func:`render_ping` (async, принимает uid).
    """
    now = datetime.now(timezone.utc)
    dt = message_date
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ping_ms = int((now - dt).total_seconds() * 1000)
    return Texts.Ping.PING.render(premium=False, ms=str(ping_ms))


async def render_ping(user_id, message_date) -> str:
    """``.ping`` — рендер с premium-aware эмодзи для данного юзера."""
    now = datetime.now(timezone.utc)
    dt = message_date
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ping_ms = int((now - dt).total_seconds() * 1000)
    return await render_for_user(user_id, Texts.Ping.PING, ms=str(ping_ms))


def format_time(user_id: str | int | None = None) -> str:
    """``.time`` — текущее время. ``user_id`` → TZ юзера; ``None`` → UTC.

    Unicode-эмодзи (НЕ premium-aware). Для premium используй :func:`render_time`.
    """
    from utils.storage import get_user_tz
    tz_label = get_user_tz(user_id) if user_id is not None else None
    now_str, _ = format_with_tz(tz_label)
    return Texts.Time.TIME.render(premium=False, now=now_str)


async def render_time(user_id, *, tz_label: str | None = None) -> str:
    """``.time`` — рендер с TZ + premium-aware эмодзи."""
    from utils.storage import get_user_tz
    label = tz_label if tz_label is not None else (get_user_tz(user_id) if user_id is not None else None)
    now_str, _ = format_with_tz(label)
    return await render_for_user(user_id, Texts.Time.TIME, now=now_str)


def format_id(chat_id, user_id, username, first_name, thread_id=None) -> str:
    """``.id`` — ID чата + юзера + топика + username.

    Unicode-эмодзи (НЕ premium-aware). User-controlled данные (username/name)
    экранируются. Для premium используй :func:`render_id`.
    """
    from utils.escape import esc
    topic_line = f"\n• Topic ID: <code>{int(thread_id)}</code>" if thread_id else ""
    return Texts.ID.INFO.render(
        premium=False,
        chat_id=str(chat_id),
        topic_line=topic_line,
        user_id=str(user_id),
        username=esc(username or "нет"),
        name=esc(first_name or ""),
    )


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


def format_help(has_session: bool = False) -> str:
    """``.help`` — скрытая справка по командам.

    Структура такая же, как раньше. Для premium используй :func:`render_help`.
    """
    title = Texts.Help.TITLE.render(premium=False)
    base = [
        "<blockquote expandable>",
        title,
        "",
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
        "",
        "<i>Подсказка:</i> <code>.команда справка</code> — детали по любой команде.",
    ]
    if has_session:
        base += [
            "",
            "<b>С сессией ещё:</b>",
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
    else:
        base += [
            "",
            "[i] Подключи <b>дополнительные возможности</b> (кнопка ниже) — появятся <code>.del</code>, <code>.save</code>, <code>.watch @user</code>, и команды будут работать везде.",
        ]
    base += [
        "",
        "[i] Отключить сессию: <b>/logout</b>.",
        "</blockquote>",
    ]
    return command_card("Help", "\n".join(base))


async def render_help(uid, has_session: bool = False) -> str:
    """``.help`` — финальный HTML со скрытым expandable-блоком."""
    title = await render_for_user(uid, Texts.Help.TITLE)
    base = [
        "<blockquote expandable>",
        title,
        "",
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
        "",
        "<i>Подсказка:</i> <code>.команда справка</code> — детали по любой команде.",
    ]
    if has_session:
        base += [
            "",
            "<b>С сессией ещё:</b>",
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
    else:
        base += [
            "",
            "[i] Подключи <b>дополнительные возможности</b> (кнопка ниже) — появятся <code>.del</code>, <code>.save</code>, <code>.watch @user</code>, и команды будут работать везде.",
        ]
    base += [
        "",
        "[i] Отключить сессию: <b>/logout</b>.",
        "</blockquote>",
    ]
    return command_card("Help", "\n".join(base))
