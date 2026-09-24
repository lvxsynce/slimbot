"""Преcеты и парсинг таймзон для команды `.timezone`.

Используется и aiogram-путем (`handlers/commands/timezone.py`),
и Telethon-путем (`utils/telethon_manager.py::_handle_timezone`).
"""
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python <3.9 fallback (только UTC).
    ZoneInfo = None
    class ZoneInfoNotFoundError(Exception):
        pass


_RESET_KEYWORDS = {"utc", "reset", "сброс", "0"}


def is_reset_value(value: str) -> bool:
    """True, если значение — команда 'сброс' (UTC / reset / сброс / 0 в любом регистре)."""
    if not value:
        return False
    return value.strip().lower() in _RESET_KEYWORDS


# ----------------- формат вывода -----------------

def format_with_tz(tz_label: str) -> tuple[str, object]:
    """Текущее время с учётом настройки таймзоны.

    Возвращает (text, tz_obj). tz_label как у юзера: '+3', 'Europe/Moscow',
    'UTC'. Невалидное значение → безопасный fallback на UTC, метка 'UTC'.
    """
    tz_obj, label = _resolve_tz(tz_label)
    now = datetime.now(tz_obj)
    return (
        "<blockquote><b>🕐 Текущее время:</b>\n"
        f"<code>{now.strftime('%Y-%m-%d %H:%M:%S')}</code> ({label})</blockquote>"
    ), tz_obj


def _resolve_tz(tz_label: Optional[str]) -> tuple:
    """Возвращает (tz_object, display_label) для строки хранилища."""
    if not tz_label:
        return timezone.utc, "UTC"
    parsed = parse_tz(tz_label)
    if parsed is None:
        return timezone.utc, "UTC"
    if isinstance(parsed, _OffsetOffset):
        offset = parsed.timedelta
        return timezone(offset), _format_offset_label(parsed)
    if ZoneInfo is not None:
        try:
            return ZoneInfo(parsed.name), parsed.name
        except (ZoneInfoNotFoundError, Exception):
            return timezone.utc, "UTC"
    return timezone.utc, "UTC"


def _format_offset_label(parsed: "_OffsetOffset") -> str:
    """'+3' → 'UTC+3', '+05:30' → 'UTC+5:30'."""
    secs = int(parsed.timedelta.total_seconds())
    sign = "+" if secs >= 0 else "-"
    secs = abs(secs)
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if m == 0:
        return f"UTC{sign}{h}"
    return f"UTC{sign}{h}:{m:02d}"


# ----------------- парcинг ввода -----------------

_OFFSET_RE = re.compile(r"^[+-](\d{1,2})(?::(\d{2}))?$")


class _OffsetOffset:
    __slots__ = ("timedelta",)

    def __init__(self, td: timedelta) -> None:
        self.timedelta = td


def parse_tz(value: str) -> Optional[object]:
    """Парсит строку → _OffsetOffset / 'IANA_NAME' / None ('UTC'-like)."""
    v = (value or "").strip()
    if not v:
        return None
    m = _OFFSET_RE.match(v)
    if m:
        hours = int(m.group(1))
        mins = int(m.group(2) or 0)
        if hours > 14 or mins >= 60:
            return None
        sign = 1 if v[0] == "+" else -1
        td = timedelta(hours=hours * sign, minutes=mins * sign)
        return _OffsetOffset(td)
    if ZoneInfo is not None:
        try:
            ZoneInfo(v)
            return _IanaName(v)
        except (ZoneInfoNotFoundError, Exception):
            return None
    return None


class _IanaName:
    __slots__ = ("name",)
    def __init__(self, name: str) -> None:
        self.name = name


def _canonicalize(value: str) -> Optional[str]:
    """Алиасы ('МСК', 'msk', 'питер') → 'Europe/Moscow' / '+3'.

    Возвращает каноничную строку для хранения в user_timezones,
    или None если алиас не найден и это не валидный оффсет/IANA.
    Note: для 'utc'/'reset'/'сброс' возвращает None — обработчик должен
    использовать is_reset_value() ДО вызова, чтобы обработать сброс явно.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower in _RESET_KEYWORDS:
        return None
    aliased = RU_ALIASES.get(lower)
    if aliased:
        return aliased
    parsed = parse_tz(raw)
    if parsed is None:
        return None
    if isinstance(parsed, _OffsetOffset):
        return _format_offset_compact(parsed)
    return parsed.name


def _format_offset_compact(parsed: "_OffsetOffset") -> str:
    """'+3' вместо '+03'/'+3:00'. Стабильное представление для JSON."""
    secs = int(parsed.timedelta.total_seconds())
    sign = "+" if secs >= 0 else "-"
    secs = abs(secs)
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if m == 0:
        return f"{sign}{h}"
    return f"{sign}{h}:{m:02d}"


# ----------------- пресеты для листинга -----------------

# (Отображаемое имя, '+N' оффсет, регион/ремарка). Используется для листинга
# в `.timezone` без аргументов.
TZ_PRESETS = [
    ("Москва / СПб", "+3", "UTC+3"),
    ("Калининград", "+2", "UTC+2"),
    ("Самара / Удмуртия", "+4", "UTC+4"),
    ("Екатеринбург", "+5", "UTC+5"),
    ("Омск", "+6", "UTC+6"),
    ("Красноярск", "+7", "UTC+7"),
    ("Иркутск", "+8", "UTC+8"),
    ("Якутск", "+9", "UTC+9"),
    ("Владивосток", "+10", "UTC+10"),
    ("Магадан / Сахалин", "+11", "UTC+11"),
    ("Камчатка / Чукотка", "+12", "UTC+12"),
    ("UTC", "+0", "по умолчанию"),
    ("Лондон / GMT", "+0", "UTC+0"),
    ("Берлин / CET", "+1", "UTC+1"),
    ("Киев / EET", "+2", "UTC+2"),
    ("Нью-Йорк / EST", "-5", "UTC−5"),
    ("Лос-Анджелес / PST", "-8", "UTC−8"),
    ("Токио / JST", "+9", "UTC+9"),
]


# Русские (и латинские) алиасы → нормализованная строка хранения.
# Некоторые явно ссылаются на '+3' (UTC+3), некоторые на IANA имя.
RU_ALIASES = {
    "мск": "+3",
    "msk": "+3",
    "спб": "+3",
    "spb": "+3",
    "питер": "+3",
    "москва": "+3",
    "moscow": "+3",
    "калининград": "+2",
    "самара": "+4",
    "удмуртия": "+4",
    "екатеринбург": "+5",
    "екб": "+5",
    "омск": "+6",
    "красноярск": "+7",
    "иркутск": "+8",
    "якутск": "+9",
    "владивосток": "+10",
    "магадан": "+11",
    "сахалин": "+11",
    "камчатка": "+12",
    "чукотка": "+12",
    "лондон": "+0",
    "germany": "+1",
    "германия": "+1",
    "берлин": "+1",
    "киев": "Europe/Kyiv",
    "украина": "Europe/Kyiv",
    "минск": "Europe/Minsk",
    "беларусь": "Europe/Minsk",
    "алматы": "Asia/Almaty",
    "казахстан": "Asia/Almaty",
    "ташкент": "Asia/Tashkent",
    "узбекистан": "Asia/Tashkent",
    "тбилиси": "Asia/Tbilisi",
    "грузия": "Asia/Tbilisi",
    "ереван": "Asia/Yerevan",
    "армения": "Asia/Yerevan",
    "баку": "Asia/Baku",
    "азербайджан": "Asia/Baku",
    "тегеран": "Asia/Tehran",
    "иран": "Asia/Tehran",
    "дубай": "Asia/Dubai",
    "оаэ": "Asia/Dubai",
    "стамбул": "Europe/Istanbul",
    "турция": "Europe/Istanbul",
    "нью-йорк": "-5",
    "newyork": "-5",
    "лос-анджелес": "-8",
    "la": "-8",
    "чикаго": "-6",
    "токио": "Asia/Tokyo",
    "япония": "Asia/Tokyo",
    "пекин": "Asia/Shanghai",
    "китай": "Asia/Shanghai",
    "сингапур": "Asia/Singapore",
    "сеул": "Asia/Seoul",
    "корея": "Asia/Seoul",
    "дели": "Asia/Kolkata",
    "индия": "Asia/Kolkata",
}
