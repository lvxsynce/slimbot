"""Логика команд, общая для Telethon- и aiogram-путей.

Зачем
----
Каждая команда реализовывалась дважды: в `handlers/commands/*` (aiogram,
личka с ботом) и прямо в `utils/telethon_manager._handle_outgoing`
(Telethon, все остальные чаты). Копии разошлись по фичам:

* ``.b64`` — aiogram-путь не знал про модификатор ``url``;
* ``.watched`` — Telethon рисовал кликабельные ссылки, aiogram нет;
* ``.ping`` — в Telethon карточка на 9 строк, в aiogram одна;
* ``.timezone`` — в Telethon была вторая копия с захардкоженными строками;
* ``.coin`` — одинаковые 6 строк в двух местах;
* подсказка ``.ии`` — три копии, две уже разошлись на строку.

Здесь лежит одна реализация на команду. Функции чистые (кроме premium-
рендера), зависят только от других ``utils`` — поэтому модуль импортируется
и из ``telethon_manager`` на уровне модуля, без циклической зависимости
(та же схема, что у ``utils.cmds``).
"""

from __future__ import annotations

from html import escape as _h

from utils.escape import esc
from utils.timezones import TZ_PRESETS, _canonicalize, is_reset_value

# ---------------------------------------------------------------------------
# .ping
# ---------------------------------------------------------------------------


def ping_fields(*, api_rtt_ms=None, get_me_rtt_ms=None, edit_rtt_ms=None,
                chat_id=None, user_id=None, dc_id=None,
                connected=None, authorized=None) -> dict:
    """Собрать измерения для карточки `.ping` (чистая функция).

    Каждый путь передаёт то, что реально может измерить: aiogram знает про
    Bot API, Telethon — про MTProto-соединение. Карточка и набор строк при
    этом общие.
    """
    def ms(value):
        return f"{value} ms" if value is not None else "—"

    return {
        "chat_id": chat_id,
        "user_id": user_id,
        "dc_id": dc_id,
        "connected": connected,
        "authorized": authorized,
        "api_rtt_ms": ms(api_rtt_ms),
        "get_me_rtt_ms": ms(get_me_rtt_ms),
        "edit_rtt_ms": ms(edit_rtt_ms),
    }


def ping_body(f: dict) -> str:
    """Тело карточки `.ping` — единое для обоих путей."""
    lines = []
    if f.get("chat_id") is not None:
        lines.append(f"Chat ID: <code>{f['chat_id']}</code>")
    if f.get("user_id") is not None:
        lines.append(f"User ID: <code>{f['user_id']}</code>")
    if f.get("dc_id") is not None:
        lines.append(f"DC: <code>{f['dc_id']}</code>")
    if f.get("connected") is not None:
        lines.append(f"Connected: <code>{'yes' if f['connected'] else 'no'}</code>")
    if f.get("authorized") is not None:
        lines.append(f"Authorized: <code>{'yes' if f['authorized'] else 'no'}</code>")
    # Значения БЕЗ <code>: обёртка принадлежит вызывающему (Telethon-путь
    # добавляет её через _ping_pairs), иначе получается двойная обёртка.
    lines.append(f"Telegram API RTT: {f['api_rtt_ms']}")
    lines.append(f"get_me RTT: {f['get_me_rtt_ms']}")
    lines.append(f"Edit RTT: {f['edit_rtt_ms']}")
    return "\n".join(lines)


def ping_from_edits(edit_rtt_ms) -> dict:
    """Карточка для aiogram-пути: измеряем только RTT собственных правок."""
    return ping_fields(edit_rtt_ms=edit_rtt_ms)


# ---------------------------------------------------------------------------
# .coin
# ---------------------------------------------------------------------------

#: Реализация живёт в handlers/commands/coin.py; здесь только формат.
COIN_ALIASES = (".монетка", ".coin", ".монета", ".орёл", ".решка")


def coin_is_heads(result: str) -> bool:
    return result == "орёл"


# ---------------------------------------------------------------------------
# .b64
# ---------------------------------------------------------------------------

_URL_FLAGS = ("url", "urlsafe", "url-safe")
_MODE_ALIASES = {
    "encode": "encode", "e": "encode", "enc": "encode",
    "decode": "decode", "d": "decode", "dec": "decode",
}


def parse_b64_args(args: str) -> tuple[str, str, bool]:
    """Разобрать аргументы `.b64` → ``(mode, text, url_safe)``.

    Раньше aiogram-путь не знал про ``url`` и выдавал другую разметку, чем
    Telethon-путь: одна и та же команда вела себя по-разному в зависимости
    от того, где её вызвали.
    """
    url_safe = False
    mode = "encode"
    text = ""
    parts = (args or "").split()
    if parts:
        if any(p.lower() in _URL_FLAGS for p in parts):
            url_safe = True
        clean = [p for p in parts if p.lower() not in _URL_FLAGS]
        if clean:
            first = clean[0].lower()
            if first in _MODE_ALIASES:
                mode = _MODE_ALIASES[first]
                text = " ".join(clean[1:])
            else:
                text = " ".join(clean)
    return mode, text, url_safe


def b64_body(mode: str, text: str, result: str, url_safe: bool) -> str:
    """Единая карточка `.b64` для обоих путей.

    Результат всегда обрезается: base64 от длинного текста легко
    переваливает за лимит Telegram, и пользователь видел бы тишину.
    """
    label = ("url-decode" if mode == "decode" else "url-encode") if url_safe else mode
    src = esc((text[:60] + ("…" if len(text) > 60 else "")))
    if mode == "decode":
        out = esc(result[:200] + ("…" if len(result) > 200 else ""))
        return (
            f"<b>🔐 b64 {esc(label)}</b>\n"
            f"<i>in:</i> <code>{src}</code>\n"
            f"<i>out ({len(result)} chars):</i> <code>{out}</code>"
        )
    out = esc(result[:2000] + ("…" if len(result) > 2000 else ""))
    return (
        f"<b>🔐 b64 {esc(label)}</b>\n"
        f"<i>in:</i> {src}\n"
        f"<i>out:</i> <code>{out}</code>"
    )


# ---------------------------------------------------------------------------
# .timezone
# ---------------------------------------------------------------------------

_TZ_EXAMPLES = (
    "<b>Примеры:</b>",
    "• <code>.timezone +3</code> — Москва, СПб",
    "• <code>.timezone -5</code> — Нью-Йорк",
    "• <code>.timezone +5:30</code> — Индия",
    "• <code>.timezone Europe/Moscow</code> — по имени",
    "• <code>.timezone МСК</code> / <code>.timezone киев</code> — алиас",
    "• <code>.timezone UTC</code> / <code>.timezone reset</code> — сброс",
    "",
)


def timezone_body(current_label: str | None, *, title: str = "<b>🌍 Часовая зона</b>") -> str:
    """Единое тело карточки `.timezone` (заголовок — от `command_card`).

    ``title`` передаётся aiogram-путём, чтобы отдать его premium-версию из
    ``Texts.Timezone.TITLE``; Telethon-путь рендерить не умеет.

    Telethon-путь раньше держал вторую копию с захардкоженными строками,
    которая разошлась с `handlers/commands/timezone.py` при первом же правке.
    """
    current = (
        f"Сейчас: <code>{esc(current_label)}</code>" if current_label
        else "Сейчас: <code>UTC</code> (по умолчанию)"
    )
    lines = [title, current, "", *_TZ_EXAMPLES, "<b>Пресеты:</b>"]
    for name, offset, remark in TZ_PRESETS:
        lines.append(f"• <b>{esc(name)}</b> (<code>{offset}</code>) — {esc(remark)}")
    lines.append("[i] Применяется к <code>.time</code>.")
    return "\n".join(lines)


def timezone_parse(args: str) -> tuple[bool, str | None]:
    """Разобрать аргумент `.timezone` → ``(ok, canonical_or_None)``."""
    if is_reset_value(args):
        return True, "0"
    if not args:
        return True, None
    canonical = _canonicalize(args)
    return (canonical is not None), canonical


# ---------------------------------------------------------------------------
# .watched
# ---------------------------------------------------------------------------

#: Сколько чатов показывать до обрезки. Список не ограничивался ни в одном
#: из путей, и 30 мёртвых чатов давали 30 неудачных RPC подряд.
WATCHED_MAX_ROWS = 40


def watched_body(rows: list[dict]) -> str:
    """Единая карточка `.watched`.

    Строка — это ``{"chat_id", "name", "url", "thread_id"}``. Если ``url``
    не задан (aiogram-путь не умеет резолвить сущности), печатается голый
    id — но в том же формате.
    """
    if not rows:
        return "[i] Список пуст. Начни с <code>.watch</code> в нужном чате."
    lines = []
    for index, row in enumerate(rows[:WATCHED_MAX_ROWS], 1):
        name = row.get("name") or f"чат #{row['chat_id']}"
        title = (
            f'<a href="{esc(row["url"])}">{esc(name)}</a>' if row.get("url")
            else f"<code>{row['chat_id']}</code>"
        )
        tid = row.get("thread_id") or 0
        suffix = f" · топик <code>{tid}</code>" if tid else " (весь чат)"
        lines.append(f"{index}. {title}{suffix}")
    if len(rows) > WATCHED_MAX_ROWS:
        lines.append(f"[i] …и ещё {len(rows) - WATCHED_MAX_ROWS}.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# .ии — подсказка по использованию
# ---------------------------------------------------------------------------

AI_HINT_LINES = (
    "<code>.ии вопрос</code> — вопрос с памятью диалога",
    "<code>.ии</code> (reply) — ответ про сообщение",
    "<code>.ии</code> (reply на фото) — анализ картинки",
    "<code>.ии сброс</code> — очистить историю",
    "<code>.ии база</code> — собрать переписки",
    "<code>.ии база вопрос</code> — поиск по базе",
)
AI_HINT_FOOTER = "<i>LLM сам вызовет .regex / .tr / .net когда нужны данные.</i>"

#: Строки, доступные только там, где есть Telethon-сессия.
AI_HINT_TELETHON_ONLY = {
    "debug": "<code>.ии дебаг вопрос</code> — то же + список тулов в ответе",
    "ctx": "<code>.ии ctx=N вопрос</code> — N сообщений вокруг реплая",
}


def ai_usage_hint(
    *,
    has_session: bool = False,
    ctx_default: int | None = None,
    history_size: int = 0,
) -> str:
    """Единая подсказка `.ии` (было три копии в трёх местах).

    Различия, которые раньше разъехались: aiogram-путь писал
    «(только в чатах)», Telethon-путь — «(по умолчанию 20)», а третья
    копия вообще без уточнений. Теперь различие выражено параметром и
    остаётся осмысленным, а не случайным.
    """
    lines = list(AI_HINT_LINES)
    if has_session:
        lines.append(AI_HINT_TELETHON_ONLY["debug"])
        ctx = f" (по умолчанию {ctx_default})" if ctx_default else ""
        lines.append(AI_HINT_TELETHON_ONLY["ctx"] + ctx)
    else:
        lines.append("<code>.ии дебаг вопрос</code> — список тулов в ответе (только в чатах)")
        lines.append("<code>.ии ctx=N вопрос</code> — N сообщений вокруг реплая (только в чатах)")
    lines.append(AI_HINT_FOOTER)
    if history_size:
        lines.append(f"\n[i] История: <code>{history_size}</code> сообщений.")
    return "\n".join(lines)


__all__ = [
    "COIN_ALIASES",
    "ai_usage_hint",
    "b64_body",
    "coin_is_heads",
    "parse_b64_args",
    "ping_body",
    "ping_fields",
    "ping_from_edits",
    "timezone_body",
    "timezone_parse",
    "watched_body",
]
