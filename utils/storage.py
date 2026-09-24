import hashlib
import json
import os
import time
import threading
from pathlib import Path
from config import (
    WATCHED_CHATS_FILE,
    USER_SESSIONS_FILE,
    PHOTO_SETTINGS_FILE,
    AUTO_TR_CHATS_FILE,
    USER_TZ_FILE,
    NYA_CHATS_FILE,
    KNOWLEDGE_SETTINGS_FILE,
    SESSIONS_DIR,
)

# user_id -> list of [chat_id, thread_id] (thread_id = 0/None для «весь чат»)
watched_chats: dict[str, list[list[int]]] = {}
user_sessions: dict[str, dict] = {}
photo_settings: dict[str, dict] = {}
# user_id -> {chat_id: target_language} для .tr auto.
# Старый формат со списком chat_id мигрируется в язык "ru" при загрузке.
auto_tr_chats: dict[str, dict[str, str]] = {}
# user_id -> '+3' или 'Europe/Moscow' (хранится как строка для простоты миграций).
user_timezones: dict[str, str] = {}
# user_id -> list of chat_id с включённым `.ня` (catgirl rewrite исходящих).
# Telethon-only фича: aiogram НЕ МОЖЕТ перехватить исходящие сообщения юзера.
# Хранится per-(user_id) без топика (форум/нет — на уровне чата).
nya_chats: dict[str, list[int]] = {}
# user_id -> {"selected_chats": [chat_id, ...]}; пустой список = сбор не запустится.
knowledge_settings: dict[str, dict] = {}

_processed_msgs: dict[tuple[int, int, int], float] = {}
_processed_last_cleanup = 0.0
_WRITE_LOCK = threading.RLock()


# ----------------- thread helpers -----------------

def _norm_thread(thread_id) -> int:
    """Нормализуем thread_id: None/0 → 0 (весь чат / не форум)."""
    if thread_id is None:
        return 0
    try:
        return int(thread_id)
    except (TypeError, ValueError):
        return 0


def thread_key(chat_id: int, thread_id) -> str:
    return f"{chat_id}:{_norm_thread(thread_id)}"


def was_processed(chat_id: int, msg_id: int, thread_id=None) -> bool:
    key = (int(chat_id), int(msg_id), _norm_thread(thread_id))
    if key in _processed_msgs:
        return True
    now = time.time()
    _processed_msgs[key] = now
    global _processed_last_cleanup
    if now - _processed_last_cleanup > 5:
        _processed_last_cleanup = now
        cutoff = now - 5
        for k in [k for k, t in _processed_msgs.items() if t < cutoff]:
            del _processed_msgs[k]
    return False


# ----------------- JSON load/save -----------------

def _atomic_write(path: Path, data):
    """Write JSON atomically and preserve the previous valid snapshot."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    backup = path.with_suffix(path.suffix + ".bak")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if path.exists():
                os.replace(path, backup)
            os.replace(tmp, path)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            if not path.exists() and backup.exists():
                os.replace(backup, path)
            raise


def _read_json(path: Path, default):
    """Read a JSON snapshot, falling back to the last valid backup."""
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        if not candidate.exists():
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return default


def _migrate_watched(old_value):
    """Старый формат: [int chat_id]; новый: [[chat_id, thread_id]].

    Поэлементная устойчивость: один битый элемент пропускается,
    остальные сохраняются (раньше исключение роняло весь словарь).
    """
    new = []
    for item in old_value:
        try:
            if isinstance(item, list) and len(item) >= 2:
                new.append([int(item[0]), _norm_thread(item[1])])
            else:
                new.append([int(item), 0])
        except (TypeError, ValueError):
            continue
    return new


def load_watched():
    global watched_chats
    if WATCHED_CHATS_FILE.exists() or WATCHED_CHATS_FILE.with_suffix(WATCHED_CHATS_FILE.suffix + ".bak").exists():
        try:
            raw = _read_json(WATCHED_CHATS_FILE, {})
            # миграция старого формата
            watched_chats = {
                str(uid): _migrate_watched(v) if isinstance(v, list) else []
                for uid, v in raw.items()
            }
            # если что-то изменилось — сохраняем новый формат
            if watched_chats != raw:
                save_watched()
        except Exception:
            watched_chats = {}
    else:
        watched_chats = {}


def save_watched():
    _atomic_write(WATCHED_CHATS_FILE, watched_chats)


def load_user_sessions():
    global user_sessions
    raw = _read_json(USER_SESSIONS_FILE, {})
    # JSON object keys are normally strings, but normalize them explicitly so
    # callers using Telegram's integer user id cannot miss a restored session.
    user_sessions = {
        str(uid): value
        for uid, value in raw.items()
        if isinstance(value, dict)
    } if isinstance(raw, dict) else {}


def save_user_sessions():
    USER_SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(USER_SESSIONS_FILE, user_sessions)


def load_photo_settings():
    global photo_settings
    photo_settings = _read_json(PHOTO_SETTINGS_FILE, {})


def save_photo_settings():
    _atomic_write(PHOTO_SETTINGS_FILE, photo_settings)


def load_auto_tr_chats():
    """Загружает настройки `.tr auto` (Telethon-сессия).
    Отдельный файл потому что функциональность не относится ни к watch,
    ни к photo, и не нужна в aiogram-слое.
    """
    global auto_tr_chats
    if AUTO_TR_CHATS_FILE.exists() or AUTO_TR_CHATS_FILE.with_suffix(AUTO_TR_CHATS_FILE.suffix + ".bak").exists():
        try:
            raw = _read_json(AUTO_TR_CHATS_FILE, {})
            migrated = {}
            for uid, value in raw.items():
                chats: dict[str, str] = {}
                if isinstance(value, list):
                    for chat_id in value:
                        try:
                            chats[str(int(chat_id))] = "ru"
                        except (TypeError, ValueError):
                            continue
                elif isinstance(value, dict):
                    for chat_id, language in value.items():
                        try:
                            chats[str(int(chat_id))] = str(language).lower()
                        except (TypeError, ValueError):
                            continue
                if chats:
                    migrated[str(uid)] = chats
            auto_tr_chats = migrated
        except Exception:
            auto_tr_chats = {}


def save_auto_tr_chats():
    AUTO_TR_CHATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(AUTO_TR_CHATS_FILE, auto_tr_chats)


# ----------------- user timezones (.timezone) -----------------

def load_user_tz():
    """user_id → str ('+3' или 'Europe/Moscow'). Минимальный валидатор: только строки."""
    global user_timezones
    if USER_TZ_FILE.exists() or USER_TZ_FILE.with_suffix(USER_TZ_FILE.suffix + ".bak").exists():
        try:
            raw = _read_json(USER_TZ_FILE, {})
            user_timezones = {str(uid): str(v) for uid, v in raw.items() if isinstance(v, str)}
        except Exception:
            user_timezones = {}


def save_user_tz():
    USER_TZ_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(USER_TZ_FILE, user_timezones)


def get_user_tz(user_id) -> str | None:
    return user_timezones.get(str(user_id))


def set_user_tz(user_id, tz: str | None) -> None:
    """Установить или сбросить (.timezone reset → None)."""
    uid = str(user_id)
    if tz is None:
        user_timezones.pop(uid, None)
    else:
        user_timezones[uid] = tz
    save_user_tz()


# ----------------- watched chats + threads -----------------

def get_chats_for_user(user_id: str) -> list[list[int]]:
    """Возвращает список [chat_id, thread_id]."""
    return watched_chats.get(user_id, [])


def is_chat_watched(user_id: str, chat_id: int, thread_id=None) -> bool:
    tid = _norm_thread(thread_id)
    for cid, ttid in watched_chats.get(user_id, []):
        if cid == chat_id and (ttid == tid or ttid == 0):
            return True
    return False


def add_chat_for_user(user_id: str, chat_id: int, thread_id=None) -> bool:
    """Добавляет [chat_id, thread_id]. Возвращает True если добавлено (не было раньше)."""
    tid = _norm_thread(thread_id)
    chats = watched_chats.setdefault(user_id, [])
    for cid, ttid in chats:
        if cid == chat_id and ttid == tid:
            return False
    chats.append([int(chat_id), tid])
    save_watched()
    return True


def remove_chat_for_user(user_id: str, chat_id: int, thread_id=None) -> bool:
    """Удаляет конкретный [chat_id, thread_id]. Если thread_id=None, удаляет все топики этого чата."""
    chats = watched_chats.get(user_id, [])
    if not chats:
        return False
    if thread_id is None:
        before = len(chats)
        chats[:] = [e for e in chats if e[0] != chat_id]
        removed = before != len(chats)
    else:
        tid = _norm_thread(thread_id)
        before = len(chats)
        chats[:] = [e for e in chats if not (e[0] == chat_id and e[1] == tid)]
        removed = before != len(chats)
    if not chats:
        watched_chats.pop(user_id, None)
    if removed:
        save_watched()
    return removed


# ----------------- photo settings -----------------

def get_photo_settings(user_id: str) -> dict:
    default = {"enabled": False, "mode": "all", "exceptions": []}
    return photo_settings.get(user_id, default)


def update_photo_settings(user_id: str, settings: dict):
    photo_settings[user_id] = settings
    save_photo_settings()


# ----------------- auto-tr chats (.tr auto, Telethon-only) -----------------

def is_auto_tr_chat(user_id: str, chat_id: int) -> bool:
    """True, если в этом чате для юзера включён `.tr auto`."""
    return str(int(chat_id)) in auto_tr_chats.get(str(user_id), {})


def get_auto_tr_chats(user_id: str) -> list[int]:
    return [int(chat_id) for chat_id in auto_tr_chats.get(str(user_id), {})]


def get_auto_tr_language(user_id: str, chat_id: int) -> str | None:
    return auto_tr_chats.get(str(user_id), {}).get(str(int(chat_id)))


def toggle_auto_tr_chat(user_id: str, chat_id: int, state: bool, language: str = "ru") -> bool:
    """Включает/выключает auto-режим и сохраняет язык назначения."""
    uid = str(user_id)
    cid = int(chat_id)
    chats = auto_tr_chats.setdefault(uid, {})
    if state:
        chats[str(cid)] = language.lower()
        save_auto_tr_chats()
        return True
    else:
        chats.pop(str(cid), None)
        if not chats:
            auto_tr_chats.pop(uid, None)
        save_auto_tr_chats()
        return False


# ----------------- .ня чаты (catgirl rewrite исходящих, Telethon-only) -----------------

def load_nya_chats():
    """Per-user список chat_id с включённым `.ня`.

    Отдельный файл потому что фича Telethon-only и не связана ни с watch,
    ни с auto-tr, ни с photo.
    """
    global nya_chats
    if NYA_CHATS_FILE.exists() or NYA_CHATS_FILE.with_suffix(NYA_CHATS_FILE.suffix + ".bak").exists():
        try:
            raw = _read_json(NYA_CHATS_FILE, {})
            parsed: dict[str, list[int]] = {}
            for uid, v in raw.items():
                if not isinstance(v, list):
                    continue
                ids = []
                for c in v:
                    try:
                        ids.append(int(c))
                    except (TypeError, ValueError):
                        continue
                if ids:
                    parsed[str(uid)] = ids
            nya_chats = parsed
        except Exception:
            nya_chats = {}
    else:
        nya_chats = {}


def save_nya_chats():
    NYA_CHATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(NYA_CHATS_FILE, nya_chats)


def is_nya_chat(user_id: str, chat_id: int) -> bool:
    """True, если в этом чате для юзера включён `.ня`."""
    return int(chat_id) in nya_chats.get(str(user_id), [])


def get_nya_chats(user_id: str) -> list[int]:
    return list(nya_chats.get(str(user_id), []))


def toggle_nya_chat(user_id: str, chat_id: int, state: bool) -> bool:
    """Включает/выключает .ня в чате. Возвращает новое состояние."""
    uid = str(user_id)
    cid = int(chat_id)
    chats = nya_chats.setdefault(uid, [])
    if state:
        if cid not in chats:
            chats.append(cid)
            save_nya_chats()
        return True
    else:
        if cid in chats:
            chats.remove(cid)
            if not chats:
                nya_chats.pop(uid, None)
            save_nya_chats()
        return False



def is_photo_allowed(user_id: str, chat_id: int, thread_id=None) -> bool:
    s = get_photo_settings(user_id)
    if not isinstance(s, dict) or not s.get("enabled"):
        return False
    mode = s.get("mode", "all")
    exceptions = s.get("exceptions", []) or []
    # исключения храним как [chat_id, thread_id]
    norm_exc = []
    for e in exceptions:
        try:
            if isinstance(e, list) and len(e) >= 2:
                norm_exc.append((int(e[0]), _norm_thread(e[1])))
            else:
                norm_exc.append((int(e), 0))
        except (TypeError, ValueError):
            continue
    tid = _norm_thread(thread_id)
    cur = (int(chat_id), tid)
    cur_chat_only = (int(chat_id), 0)
    in_exc = cur in norm_exc or cur_chat_only in norm_exc
    if mode == "all":
        # by design: в режиме "all" исключения не действуют
        # (переход на исключения идёт через all_except в watch.py).
        return True
    elif mode == "all_except":
        return not in_exc
    elif mode == "only_selected":
        return in_exc
    return False


# ----------------- sessions -----------------

def session_path(uid: str) -> str:
    h = hashlib.sha256(uid.encode()).hexdigest()[:16]
    return str(SESSIONS_DIR / h)


def session_exists(user_id: str) -> bool:
    uid = str(user_id)
    s = user_sessions.get(uid, {})
    return s.get("status") == "active" and Path(session_path(uid) + ".session").exists()


# ----------------- knowledge base selected chats -----------------

def load_knowledge_settings():
    global knowledge_settings
    raw = _read_json(KNOWLEDGE_SETTINGS_FILE, {})
    parsed: dict[str, dict] = {}
    if isinstance(raw, dict):
        for uid, value in raw.items():
            selected = value.get("selected_chats", []) if isinstance(value, dict) else []
            ids = []
            for chat_id in selected:
                try:
                    ids.append(int(chat_id))
                except (TypeError, ValueError):
                    continue
            parsed[str(uid)] = {"selected_chats": list(dict.fromkeys(ids))}
    knowledge_settings = parsed


def save_knowledge_settings():
    _atomic_write(KNOWLEDGE_SETTINGS_FILE, knowledge_settings)


def get_knowledge_selected_chats(user_id: str) -> set[int]:
    value = knowledge_settings.get(str(user_id), {})
    return {int(chat_id) for chat_id in value.get("selected_chats", [])}


def toggle_knowledge_chat(user_id: str, chat_id: int) -> bool:
    uid = str(user_id)
    selected = get_knowledge_selected_chats(uid)
    cid = int(chat_id)
    if cid in selected:
        selected.remove(cid)
        enabled = False
    else:
        selected.add(cid)
        enabled = True
    knowledge_settings[uid] = {"selected_chats": sorted(selected)}
    save_knowledge_settings()
    return enabled


load_watched()
load_user_sessions()
load_photo_settings()
load_auto_tr_chats()
load_user_tz()
load_nya_chats()
load_knowledge_settings()
