"""@bot @username — карточка профиля через Telethon.

Шаблон запроса: `@bot @username` или `@bot -100…` (по numeric ID).
Можно несколько юзернеймов через пробел/запятую: `@bot @user1 @user2 @user3`.

Требует Telethon-сессию: резолв идёт через `telethon_manager.get_client(uid)`.
Безопасность: все user-controlled поля экранируются через `_esc()` перед
вставкой в HTML-текст (FIX H2 из code-review).
"""

import asyncio
import re

from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import User as TUser

from utils.bot_info import bot_username_at
from utils.storage import session_exists
from utils.telethon_manager import telethon_manager

from . import connect_button
from config import (
    DOT_TARGET_LIMIT as INLINE_USER_LIMIT,
    TELETHON_RESOLVE_TIMEOUT as TELETHON_TIMEOUT,
)


CACHE_TIME_SINGLE = 300
CACHE_TIME_MULTI = 180


def is_profile_query(q: str) -> bool:
    return q.strip().startswith("@")


_TARGET_SPLIT_RE = re.compile(r"[\s,]+")


def parse_inline_targets(query: str, limit: int = INLINE_USER_LIMIT) -> list[str]:
    """Парсит несколько @username/ID из inline-запроса.

    Примеры:
      '@user1 @user2' → ['user1', 'user2']
      '@u1,@u2 u3' → ['u1', 'u2', 'u3']
      'user1 u2' → ['user1', 'u2']
      '-1001234567' → ['-1001234567']

    Дедуплицирует case-insensitive, возвращает максимум `limit` штук
    в порядке ввода.
    """
    if not query:
        return []
    tokens = _TARGET_SPLIT_RE.split(query.strip())
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        cleaned = t.lstrip("@").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _esc(s) -> str:
    """HTML-escape для user-controlled полей (first_name, last_name, ...)."""
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _make_article(
    title: str,
    description: str,
    text: str,
    id_: str,
) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=id_,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=text,
            parse_mode="HTML",
        ),
    )


def _extract_telethon_usernames(entity, full_user) -> list[str]:
    """Собирает активные usernames: collectibles (Premium) + legacy fallback.

    Приоритет: сначала collectibles в порядке их объявления в профиле,
    потом legacy `entity.username` если он не дублирует collectible.
    """
    out: list[str] = []
    if full_user is not None:
        for u in getattr(full_user, "usernames", None) or []:
            active = getattr(u, "active", False)
            uname = getattr(u, "username", None)
            if active and uname and uname not in out:
                out.append(uname)
    legacy = getattr(entity, "username", None)
    if legacy and legacy not in out:
        out.append(legacy)
    return out


def _format_usernames_line(unames: list[str]) -> str:
    if not unames:
        return ""
    if len(unames) == 1:
        return f"Username: @{unames[0]}"
    return "Usernames: " + " ".join(f"@{u}" for u in unames)


def _format_profile(entity, full_user) -> str:
    """Собираем HTML-карточку профиля. Все user-controlled поля — escaped."""
    lines: list[str] = []

    if isinstance(entity, TUser):
        first = _esc(getattr(entity, "first_name", None) or "")
        last = _esc(getattr(entity, "last_name", None) or "")
        full_name = (f"{first} {last}").strip() or "—"
        is_bot = bool(getattr(entity, "bot", False))
        kind = "🤖 Бот" if is_bot else "👤 Пользователь"

        lines.append(f"<b>🔍 Профиль</b> ({kind})")
        lines.append(f"ID: <code>{entity.id}</code>")
        lines.append(f"Имя: {full_name}")
        unames = _extract_telethon_usernames(entity, full_user)
        if unames:
            unames_esc = " ".join(f"@{_esc(u)}" for u in unames)
            label = "Username" if len(unames) == 1 else "Usernames"
            lines.append(f"{label}: {unames_esc}")
        phone = getattr(entity, "phone", None)
        if phone:
            lines.append(f"Телефон: +{_esc(phone)}")
        lang = getattr(entity, "lang_code", None)
        if lang:
            lines.append(f"Язык: <code>{_esc(lang)}</code>")
        if getattr(entity, "premium", False):
            lines.append("Premium: ⭐ да")
        if getattr(entity, "verified", False):
            lines.append("✓ verified")
        if getattr(entity, "scam", False):
            lines.append("⚠ scam")
        if getattr(entity, "fake", False):
            lines.append("⚠ fake")

        if full_user is not None:
            about = getattr(full_user, "about", None) or ""
            if about:
                short = about.strip().replace("\n", " ")
                if len(short) > 200:
                    short = short[:200] + "…"
                lines.append("")
                lines.append(f"<i>{_esc(short)}</i>")
            common = getattr(full_user, "common_chats_count", None)
            if isinstance(common, int) and common:
                lines.append(f"Общие чаты: {common}")
    else:
        title = _esc(getattr(entity, "title", None) or "—")
        ctype = _esc(type(entity).__name__)
        lines.append(f"<b>💬 Чат</b> ({ctype})")
        lines.append(f"ID: <code>{entity.id}</code>")
        lines.append(f"Название: {title}")
        # Chat также может иметь collectible usernames через GetFullChannelRequest;
        # в Telethon это объект ChannelFull. Здесь у нас один entity без full_chat,
        # так что показываем только legacy.
        username = _esc(getattr(entity, "username", None))
        if username:
            lines.append(f"Username: @{username}")
        members = getattr(entity, "participants_count", None)
        if members is not None:
            lines.append(f"Участников: {members}")
        flags = []
        if getattr(entity, "verified", False):
            flags.append("✓ verified")
        if getattr(entity, "scam", False):
            flags.append("⚠ scam")
        if getattr(entity, "fake", False):
            flags.append("⚠ fake")
        if getattr(entity, "restricted", False):
            flags.append("⚠ restricted")
        if flags:
            lines.append("Флаги: " + ", ".join(flags))

    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


async def _resolve_one(client, target: str) -> tuple:
    """Резолвит один target через Telethon. Returns (entity, full_user, error_text)."""
    try:
        entity = await asyncio.wait_for(
            client.get_entity(target),
            timeout=TELETHON_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return (None, None, f"telethon не ответил за {TELETHON_TIMEOUT}с")
    except Exception as e:
        return (None, None, f"{type(e).__name__}: {e}")

    full_user = None
    if isinstance(entity, TUser):
        try:
            full_user = await asyncio.wait_for(
                client(GetFullUserRequest(entity.id)),
                timeout=TELETHON_TIMEOUT,
            )
        except Exception:
            full_user = None

    return (entity, full_user, None)


def _hint_text() -> tuple[str, str, str]:
    """(title, description, text) для пустого запроса @bot @."""
    bot = bot_username_at()
    return (
        "Введи @username после бота",
        f"{bot} @durov — карточка профиля",
        (
            "<b>🔍 Карточка профиля</b>\n\n"
            f"Введи <code>{bot} @username</code> — например <code>{bot} @durov</code>.\n\n"
            f"Можно несколько: <code>{bot} @a @b @c</code>.\n\n"
            "<i>Резолв через твою Telethon-сессию.</i>"
        ),
    )


def _need_session_text() -> tuple[str, str, str]:
    bot = bot_username_at()
    return (
        "Нужна Telethon-сессия",
        "Подключи Telethon, чтобы бот резолвил @юзернеймы.",
        (
            "<b>🔍 Нужен Telethon</b>\n\n"
            f"Inline <code>{bot} @username</code> резолвит через твою Telethon-сессию. "
            f"Подключи её в личке с <b>{bot}</b> через <b>/start</b>.\n\n"
            f"Без сессии в инлайне доступны только <code>{bot} статус</code> "
            f"и <code>{bot} помощь</code>."
        ),
    )


async def handle(inline: InlineQuery) -> None:
    """Обрабатывает inline-запрос @bot @<user1> @<user2> …."""
    targets = parse_inline_targets(inline.query)
    uid = str(inline.from_user.id)
    bot = bot_username_at()

    # Пустой target / hint
    if not targets:
        title, desc, text = _hint_text()
        await inline.answer(
            results=[
                _make_article(
                    title=title,
                    description=desc,
                    text=text,
                    id_="profile_hint",
                ),
            ],
            cache_time=60,
            is_personal=True,
        )
        return

    # Авторизация
    if not session_exists(uid):
        title, desc, text = _need_session_text()
        await inline.answer(
            results=[
                _make_article(
                    title=title,
                    description=desc,
                    text=text,
                    id_="profile_need_session",
                ),
            ],
            cache_time=5,
            is_personal=True,
            button=connect_button(),
        )
        return

    client = telethon_manager.get_client(uid)
    if not client:
        await inline.answer(
            results=[],
            cache_time=5,
            is_personal=True,
        )
        return

    results: list = []
    seen_ids: set[str] = set()

    def _unique_id(base: str) -> str:
        """Уникальный id результата ≤64 символов (лимит Telegram).

        Разные написания (@durov vs durov) могут резолвиться в одну
        сущность — дублирующийся id роняет весь ответ BadRequest'ом.
        """
        cand = base[:64]
        i = 2
        while cand in seen_ids:
            suffix = f"_{i}"
            cand = base[:64 - len(suffix)] + suffix
            i += 1
        seen_ids.add(cand)
        return cand

    for target in targets:
        entity, full_user, err = await _resolve_one(client, target)
        if err is not None:
            results.append(_make_article(
                title=f"Ошибка: {target}",
                description=err[:80],
                text=(
                    f"🔍 <b>{_esc(target)}</b>\n\n"
                    f"<code>{_esc(err)}</code>"
                ),
                id_=_unique_id(f"profile_err_{target}"),
            ))
            continue

        text = _format_profile(entity, full_user)
        name = (
            getattr(entity, "first_name", None)
            or getattr(entity, "title", None)
            or target
        )
        description_bits = [f"ID: {entity.id}"]
        u = getattr(entity, "username", None)
        if u:
            description_bits.append(f"@{u}")
        p = getattr(entity, "phone", None)
        if p:
            description_bits.append(f"+{p}")

        results.append(_make_article(
            title=name,
            description=" · ".join(description_bits),
            text=text,
            id_=_unique_id(f"profile_{entity.id}"),
        ))

    if not results:
        await inline.answer(
            results=[],
            cache_time=5,
            is_personal=True,
        )
        return

    cache = CACHE_TIME_SINGLE if len(results) == 1 else CACHE_TIME_MULTI
    await inline.answer(
        results=results,
        cache_time=cache,
        is_personal=True,
    )
