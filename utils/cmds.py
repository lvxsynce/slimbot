"""Единый реестр dot-команд и их алиасов.

Зачем он нужен
--------------
Диспетч Telethon-пути (`utils/telethon_manager.py::_handle_outgoing`) раньше
содержал СВОИ хардкод-кортежи алиасов, а каждая команда в
`handlers/commands/*` — ещё и свои константы `*_CMDS` / `_check()`. Итог:
девять списков, ни одного теста-guard'а, и реальный дрейф (`.пер` был
отсутствовать в `_helpdb._PAIRS` и в списке `.вгф`).

Особенность: `telethon_manager` импортирует `handlers.commands.*` лениво
(внутри функций) — иначе возникает циклическая зависимость. Поэтому
реестр вынесен в ЛИСТОВОЙ модуль без единого импорта проекта: его можно
безопасно импортировать откуда угодно.

Использование:
    from utils.cmds import CMDS, CMDS_AI, CMDS_NETWORK, find_cmd
"""

from typing import Iterable  # noqa: F401  (используется в type-hints ниже)

# --------------------------------------------------------------------------
# Telethon-only команды (нет aiogram-роутера, вызываются из _handle_outgoing)
# --------------------------------------------------------------------------

DEL_CMDS = (".del", ".удалить")
DM_CMDS = (".влс", ".vls", ".лс", ".dm")
TAGALL_CMDS = (".tagall", ".тегвсех", ".все")
ADMINS_CMDS = (".admins", ".админы")
INVITE_CMDS = (".ссылка", ".invitelink", ".инвайт", ".invite")
PIN_CMDS = (".pin", ".закрепить", ".закреп")
UNPIN_CMDS = (".unpin", ".открепить", ".раскрепить", ".откреп")
VGF_CMDS = (".вгф", ".vfg", ".gif")
NYA_CMDS = (".ня",)
QUOTE_CMDS = (".quote", ".цитата", ".q", ".цит")
GOVNO_CMDS = (".govno", ".говно")

# `.шаб` / `.+шаб` / `.-шаб` — шаблоны сообщений.
SHAB_BASE_CMDS = (".шаб", ".шаблон", ".template", ".tmpl", ".tpl")
SHAB_ADD_CMDS = (".+шаб", ".+шаблон", ".+template", ".+tmpl", ".+tpl")
SHAB_DEL_CMDS = (".-шаб", ".-шаблон", ".-template", ".-tmpl", ".-tpl")
TEMPLATE_CMDS = SHAB_BASE_CMDS + SHAB_ADD_CMDS + SHAB_DEL_CMDS

# --------------------------------------------------------------------------
# Команды, доступные в обоих путях
# --------------------------------------------------------------------------

PING_CMDS = (".ping", ".пинг")
TIME_CMDS = (".time", ".время")
ID_CMDS = (".id", ".инфо")
ME_CMDS = (".me", ".я")
CHAT_CMDS = (".chat", ".чат")
WHO_CMDS = (".who", ".кто")
LOVE_CMDS = (".love", ".любовь")
HELP_CMDS = (".help", ".помощь")
COIN_CMDS = (".монетка", ".coin", ".монета", ".орёл", ".решка")
WATCH_CMDS = (".watch", ".следить")
UNWATCH_CMDS = (".unwatch", ".хватит", ".забыть")
WATCHED_CMDS = (".watched", ".список")
NET_CMDS = (".net", ".сеть", ".сет")
HASH_CMDS = (".hash", ".хеш", ".хэш")
UUID_CMDS = (".uuid", ".юид")
B64_CMDS = (".b64", ".base64")
TIMEZONE_CMDS = (".timezone", ".таймзона", ".tz")
TR_CMDS = (".tr", ".перевод", ".пер", ".перевести")
CALC_CMDS = (".calc", ".калк")
SAVE_CMDS = (".save", ".сохранить")
AI_CMDS = (".ии", ".ai", ".ии?")
OPENCODE_CMDS = (".опенкод", ".opencode")

# --------------------------------------------------------------------------
# Бюджеты rate-limit (см. utils/rate_limit.py)
# --------------------------------------------------------------------------

#: Вызовы LLM. Каждый стоит денег и не требует авторизации.
CMDS_AI: tuple[str, ...] = AI_CMDS + TR_CMDS

#: Сетевые: DNS, TLS, скачивание. Несколько исходящих запросов на вызов.
CMDS_NETWORK: tuple[str, ...] = NET_CMDS

#: Тяжёлый CPU + ffmpeg: десятки секунд, процессы в RAM.
#: `.quote`/`.вгф` поднимают ffmpeg, `.quote` ещё и держит 40 RGBA-кадров.
CMDS_HEAVY: tuple[str, ...] = VGF_CMDS + QUOTE_CMDS + OPENCODE_CMDS

#: Анимации: 21 `editMessageText` каждый. Едят FloodWait-бюджет ОБЩЕГО
#: токена бота, то есть бьют по ВСЕМ пользователям сразу.
CMDS_ANIM: tuple[str, ...] = LOVE_CMDS + GOVNO_CMDS

#: Локальные вычисления (CPU пользователя, сеть не трогаем).
CMDS_LOCAL: tuple[str, ...] = HASH_CMDS + UUID_CMDS + B64_CMDS + CALC_CMDS

#: Всё, что ограничиваем. Отдельные бюджеты намеренно: общий «network»
#: позволял пяти дешёвым `.love` заблокировать `.net` на минуту.
CMDS_RATELIMITED: tuple[str, ...] = (
    CMDS_AI + CMDS_NETWORK + CMDS_HEAVY + CMDS_ANIM + CMDS_LOCAL
)

#: Полный список dot-команд, обрабатываемых Telethon-путём.
CMDS: tuple[str, ...] = (
    PING_CMDS + TIME_CMDS + ID_CMDS + ME_CMDS + CHAT_CMDS + WHO_CMDS
    + LOVE_CMDS + GOVNO_CMDS + HELP_CMDS + COIN_CMDS
    + WATCH_CMDS + UNWATCH_CMDS + WATCHED_CMDS
    + NET_CMDS + DEL_CMDS + TR_CMDS + CALC_CMDS + SAVE_CMDS
    + HASH_CMDS + UUID_CMDS + B64_CMDS + TIMEZONE_CMDS
    + TAGALL_CMDS + DM_CMDS + ADMINS_CMDS + PIN_CMDS + UNPIN_CMDS
    + INVITE_CMDS + QUOTE_CMDS + TEMPLATE_CMDS + NYA_CMDS + VGF_CMDS
    + AI_CMDS + OPENCODE_CMDS
)

_ALL: frozenset[str] = frozenset(CMDS)


def is_known(command: str) -> bool:
    """Известна ли команда — dot (``.ping``) или slash (``/help``)."""
    value = str(command or "").strip().lower()
    return value in _ALL or value in SLASH_CMDS


#: Раздельные бюджеты (для тестов и документации).
#: CMDS_* — уже плоские кортежи строк, поэтому frozenset() напрямую:
#: set("ab") дал бы множество символов.
AI_BUDGET: frozenset[str] = frozenset(CMDS_AI)
NETWORK_BUDGET: frozenset[str] = frozenset(CMDS_NETWORK)
HEAVY_BUDGET: frozenset[str] = frozenset(CMDS_HEAVY)
ANIM_BUDGET: frozenset[str] = frozenset(CMDS_ANIM)
LOCAL_BUDGET: frozenset[str] = frozenset(CMDS_LOCAL)
RATE_LIMITED_BUDGET: frozenset[str] = frozenset(CMDS_RATELIMITED)

#: Slash-команды aiogram-слоя (живут в личке с ботом, не dot-команды).
SLASH_CMDS: frozenset[str] = frozenset({"/start", "/help", "/status", "/logout"})
