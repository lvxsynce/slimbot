"""Центральный config для slimbot.

Загружается из переменных окружения (опционально — из .env в корне проекта
через python-dotenv, если установлен). Все defaults — backwards-compatible:
если env не задан, используется прежнее захардкоженное значение.

Секреты (Tier 1) — без `.env` бот НЕ упадёт, но `_check_secrets()` пошлёт
⚠️-warning в логи. Рекомендуется сразу перенести их в .env и добавить
`.gitignore` строку `.env`, чтобы случайно не закоммитить реальные креды.

Все runtime-tunables (Tier 3) следуют одной идиоме:
    _INT_ENV(name, default) = int(os.getenv(name, str(default)))
    _FLOAT_ENV(name, default) = float(os.getenv(name, str(default)))
Это даёт единый тип Python и автоматический валидационный crash на старте
(если в .env положили мусор вместо числа).
"""

import os
from pathlib import Path


# Runtime state must not depend on the directory from which systemd/docker
# starts the process.  The old relative paths made an otherwise valid
# Telethon session look missing after a service restart.
PROJECT_DIR = Path(__file__).resolve().parent
_data_dir = Path(os.getenv("SLIMBOT_DATA_DIR", str(PROJECT_DIR))).expanduser()
DATA_DIR = (_data_dir if _data_dir.is_absolute() else PROJECT_DIR / _data_dir).resolve()

# Auto-load .env from project root if python-dotenv is available.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ----------------- helpers -----------------

def _int_env(name: str, default: int) -> int:
    """Читает int из env (fallback — строковый default)."""
    return int(os.getenv(name, str(default)))


def _float_env(name: str, default: float) -> float:
    """Читает float из env (fallback — строковый default)."""
    return float(os.getenv(name, str(default)))


# ----------------- Tier 1: Secrets -----------------
# Живых значений здесь НЕТ (и не должно быть — репозиторий публичный).
# Все секреты задаются через environment / .env (см. .env.example).
# ENV: TOKEN — Telegram bot token (BotFather).
TOKEN = os.getenv("TOKEN", "")
# ENV: API_ID / API_HASH — Telethon креды (my.telegram.org).
API_ID = _int_env("API_ID", 0)
API_HASH = os.getenv("API_HASH", "")
# Fixed AI backend. Values are configured in `.env` and are not user-switchable.
AI_API_KEY = os.getenv("AI_API_KEY_WILLOW", "").strip()


def _check_secrets() -> None:
    """Если SECRET не задан — печатает ⚠️ на старте (не падает)."""
    issues: list[str] = []
    if not TOKEN or ":" not in TOKEN:
        issues.append("TOKEN")
    if not API_ID:
        issues.append("API_ID")
    if not API_HASH:
        issues.append("API_HASH")
    if not AI_API_KEY:
        issues.append("AI_API_KEY_WILLOW")
    if issues:
        import logging
        logging.getLogger(__name__).warning(
            "⚠️ Не заданы секреты (заполни .env!): " + ", ".join(issues)
            + " — настоящие креды должны быть в .env, а не в config.py."
        )


# ----------------- Tier 2: Endpoints & external services -----------------

LLM_TIMEOUT = _int_env("LLM_TIMEOUT", 60)
LLM_TEMPERATURE = _float_env("LLM_TEMPERATURE", 0.5)
LLM_MAX_TOKENS = _int_env("LLM_MAX_TOKENS", 4000)

# Fixed backend settings for `.ии` and AI translation.
AI_URL = os.getenv("WILLOW_CHAT_URL", "https://api.willowapi.digital/v1/chat/completions")
AI_MODEL = os.getenv("WILLOW_MODEL", "gpt-5.6-luna")
AI_REASONING_EFFORT = os.getenv("WILLOW_REASONING_EFFORT", "low")

# ipinfo.io — geo/ASN по IP.
IPINFO_URL_TEMPLATE = os.getenv("IPINFO_URL_TEMPLATE", "https://ipinfo.io/{ip}/json")

# HTTP client timeouts (DNS, ipinfo, linkcheck, unshorten — общая природа).
NETINFO_TIMEOUT_TOTAL = _int_env("NETINFO_TIMEOUT_TOTAL", 12)
NETINFO_TIMEOUT_CONNECT = _int_env("NETINFO_TIMEOUT_CONNECT", 6)
LINKCHECK_TIMEOUT_TOTAL = _int_env("LINKCHECK_TIMEOUT_TOTAL", 12)
LINKCHECK_TIMEOUT_CONNECT = _int_env("LINKCHECK_TIMEOUT_CONNECT", 6)

# UA — каноничный вариант из linkcheck.py: (KHTML, like Gecko) подсказка
# помогает против самых примитивных антибот-фильтров.
DEFAULT_USER_AGENT = os.getenv(
    "DEFAULT_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
)

# Bot identity (для inline'а — runtime resolved; здесь fallback на этапе первого импорта).
BOT_NAME = "Slim bot"
DEFAULT_BOT_USERNAME = os.getenv("DEFAULT_BOT_USERNAME", "slimbot")


# ----------------- Tier 3: Runtime tunables -----------------

# Message cache removed with Business API (deletions tracking was Business-only).

# .ии dialog history (per chat).
MAX_HISTORY = _int_env("MAX_HISTORY", 40)
AI_MEMORY_FILE = DATA_DIR / "ai_memory.json"
AI_MEMORY_MAX_CHARS = _int_env("AI_MEMORY_MAX_CHARS", 2000)
AI_MEMORY_MAX_TOPICS = _int_env("AI_MEMORY_MAX_TOPICS", 500)
AI_MEMORY_TTL = _int_env("AI_MEMORY_TTL", 2592000)

# .net link analysis and unshorten follow hops.
MAX_REDIRECTS = _int_env("MAX_REDIRECTS", 10)
NET_PASSIVE_TIMEOUT = _int_env("NET_PASSIVE_TIMEOUT", 12)
NET_PASSIVE_MAX_NAMES = _int_env("NET_PASSIVE_MAX_NAMES", 30)

# Telethon auth flow TTL — зависший номер/код.
AUTH_STATE_TTL = _int_env("AUTH_STATE_TTL", 300)

# Auth-state cleanup loop period (seconds).
AUTH_CLEAN_INTERVAL = _int_env("AUTH_CLEAN_INTERVAL", 60)
# Auth attempts are limited per user to reduce abuse and Telegram FloodWaits.
AUTH_MAX_ATTEMPTS = _int_env("AUTH_MAX_ATTEMPTS", 5)
AUTH_ATTEMPT_WINDOW = _int_env("AUTH_ATTEMPT_WINDOW", 600)
AUTH_COOLDOWN = _int_env("AUTH_COOLDOWN", 60)
# Expensive command limits, applied per user and operation.
EXPENSIVE_COMMAND_LIMIT = _int_env("EXPENSIVE_COMMAND_LIMIT", 6)
EXPENSIVE_COMMAND_WINDOW = _int_env("EXPENSIVE_COMMAND_WINDOW", 60)
AI_REQUEST_LIMIT = _int_env("AI_REQUEST_LIMIT", 10)
AI_REQUEST_WINDOW = _int_env("AI_REQUEST_WINDOW", 60)

# .tr auto throttle — задержка между reply в одном чате.
AUTO_TR_DELAY = _float_env("AUTO_TR_DELAY", 0.5)

# .ня (catgirl-rewrite исходящих сообщений юзера в конкретном чате).
# Tunables для процедурного rewriter'а в utils/catgirl.py.
# Дефолтные probabilities (повышены в v3) — пользователь ожидает ЧАСТОГО
# срабатывания catgirl-стиля. Если все gates сложатся в 0 — финальная
# страховка (в utils/catgirl.py::to_catgirl) всё равно добавит kaomoji 100%.
NYA_STUTTER_PROB = _float_env("NYA_STUTTER_PROB", 0.60)       # вероятность заикания первого слога
NYA_VOWEL_DOUBLE_PROB = _float_env("NYA_VOWEL_DOUBLE_PROB", 0.40)  # удвоение первой гласной (русские слова на vowel)
NYA_ACTION_PROB = _float_env("NYA_ACTION_PROB", 0.45)         # *action* префикс
NYA_KAOMOJI_PROB = _float_env("NYA_KAOMOJI_PROB", 0.55)        # каомодзи суффикс
NYA_MIN_LEN = _int_env("NYA_MIN_LEN", 4)                      # min длина текста для rewrite (если короче — skip)
NYA_EDIT_DELAY = _float_env("NYA_EDIT_DELAY", 0.6)           # задержка (с) перед event.edit в _apply_nya — даём основной hook-цепочке завершиться

# Лимит целей для .who @a @b @c / inline @user1 @user2 @user3.
DOT_TARGET_LIMIT = _int_env("DOT_TARGET_LIMIT", 5)

# Telethon entity-resolve timeout (get_entity, GetFullUserRequest, GetFullChannelRequest).
TELETHON_RESOLVE_TIMEOUT = _int_env("TELETHON_RESOLVE_TIMEOUT", 10)
# Telethon send timeout (send_message / send_file).
TELETHON_SEND_TIMEOUT = _int_env("TELETHON_SEND_TIMEOUT", 30)
# Reconnect backoff for a client that disconnects unexpectedly.
TELETHON_RECONNECT_MAX_DELAY = _int_env("TELETHON_RECONNECT_MAX_DELAY", 60)
# Periodic liveness check for restored and long-running clients.
TELETHON_HEALTH_INTERVAL = _int_env("TELETHON_HEALTH_INTERVAL", 60)
TELETHON_HEALTH_TIMEOUT = _int_env("TELETHON_HEALTH_TIMEOUT", 15)

# Inline cache_time для default dispatcher'а (handlers/inline/__init__.py).
INLINE_CACHE_DEFAULT = _int_env("INLINE_CACHE_DEFAULT", 60)

# AI .ии — extended features
# Максимум раундов автокоманд (.who / .net / .tr и т.п.) за один запрос LLM.
AI_TOOL_MAX_ITERATIONS = _int_env("AI_TOOL_MAX_ITERATIONS", 3)
# Лимит сообщений для regex-поиска по чату (scan depth).
AI_REGEX_SEARCH_SCAN_LIMIT = _int_env("AI_REGEX_SEARCH_SCAN_LIMIT", 200)
# Максимум результатов, передаваемых в LLM.
AI_REGEX_SEARCH_MAX_RESULTS = _int_env("AI_REGEX_SEARCH_MAX_RESULTS", 10)
# Глубина walk-up по reply-цепочке.
AI_DEEP_REPLY_MAX_DEPTH = _int_env("AI_DEEP_REPLY_MAX_DEPTH", 10)
# Максимальная длина одного результата автокоманды перед триммингом.
AI_TOOL_OUTPUT_MAX_CHARS = _int_env("AI_TOOL_OUTPUT_MAX_CHARS", 800)

# `.ии ctx=N` / `context_len=N` (Telethon-only): сколько сообщений
# собрать вокруг реплая (±N). Default — текущее историческое поведение
# (±20). MAX — clamp, чтобы LLM не утонула в огромном контексте.
# aiogram-путь параметр проглатывает как no-op (нет MTProto).
AI_CONTEXT_LEN_DEFAULT = _int_env("AI_CONTEXT_LEN_DEFAULT", 20)
AI_CONTEXT_LEN_MAX = _int_env("AI_CONTEXT_LEN_MAX", 200)

# `.ии база` — локальный индекс текстовых переписок Telethon.
# История идёт последовательно, чтобы не создавать лишнюю нагрузку на Telegram.
KNOWLEDGE_PROGRESS_INTERVAL = _int_env("KNOWLEDGE_PROGRESS_INTERVAL", 20)
KNOWLEDGE_CHECKPOINT_EVERY = _int_env("KNOWLEDGE_CHECKPOINT_EVERY", 100)
KNOWLEDGE_HISTORY_WAIT_TIME = _float_env("KNOWLEDGE_HISTORY_WAIT_TIME", 1.0)
KNOWLEDGE_SEARCH_LIMIT = _int_env("KNOWLEDGE_SEARCH_LIMIT", 8)
KNOWLEDGE_CONTEXT_MAX_CHARS = _int_env("KNOWLEDGE_CONTEXT_MAX_CHARS", 12000)
# Потолок сохранённых сообщений на одного владельца базы знаний. Retention:
# когда в .ии база набрано больше, старые записи вычищаются при чекпоинтах
# сбора (защита от неограниченного роста knowledge.sqlite3).
KNOWLEDGE_MAX_MESSAGES_PER_OWNER = _int_env("KNOWLEDGE_MAX_MESSAGES_PER_OWNER", 200_000)

# Whitelist read-only команд, которые LLM может попросить вызвать через
# синтаксис `<tool_use>cmd args</tool_use>` (см. handlers/commands/ai.py::_TOOL_RE
# и utils/ai_prompts.py::SYSTEM_TOOLS). Любая команда, не входящая в этот
# список, на этапе _llm_iterate получит [rejected] и не выполнится.
# Это TELLETHON-ONLY фича, к whitelisted командам относятся read-only операции.
# Любая side-effect команда (.del, .pin, .tagall, .dm, .watch, .admins, .save, .invitelink)
# ЯВНО отсутствует и будет отклонена парсером.
# .regex — поиск по чату по regex-pattern'у (LLM-инструмент, не user-side).
AI_TOOL_ALLOWLIST = frozenset({
    "who", "net", "tr", "hash", "uuid", "b64",
    "calc", "time", "ping", "id", "me", "chat",
    "regex",
})


# ----------------- Files & dirs -----------------

WATCHED_CHATS_FILE = DATA_DIR / "watched_chats.json"
USER_SESSIONS_FILE = DATA_DIR / "user_sessions.json"
PHOTO_SETTINGS_FILE = DATA_DIR / "photo_settings.json"
AUTO_TR_CHATS_FILE = DATA_DIR / "auto_tr_chats.json"
USER_TZ_FILE = DATA_DIR / "user_timezones.json"
NYA_CHATS_FILE = DATA_DIR / "nya_chats.json"
KNOWLEDGE_DB_FILE = DATA_DIR / "knowledge.sqlite3"
KNOWLEDGE_SETTINGS_FILE = DATA_DIR / "knowledge_settings.json"

SESSIONS_DIR = DATA_DIR / "sessions"
TEMP_DIR = DATA_DIR / "temp"


# ----------------- Bot identity (resolved at runtime) -----------------

# Заполняется в bot.py::on_startup() после `await bot.get_me()`.
BOT_USERNAME = None


# ----------------- Final sanity check -----------------

_check_secrets()
