"""Состояние модуля: маленький per-user JSON.

Модулю нужен способ запомнить что-то между вызовами команды (курсор,
избранные чаты, последний запрос). Давать ему `utils.storage` целиком —
плохая идея: там чужие словари (сессии, шаблоны) и случайная запись в
них ломает другие фичи.

Поэтому модуль получает `ctx.state` с методами `get/set/delete/all`.
Данные лежат в `DATA_DIR/module_state/<uid_hash>_<module>.json` и
переживают перезапуск бота.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.RLock()

#: uid -> {module: dict} — кеш, чтобы не читать файл на каждый get.
_cache: dict[str, dict] = {}


def _hash_uid(uid: str) -> str:
    return hashlib.sha256(str(uid).encode()).hexdigest()[:16]


def state_path(uid: str, module: str) -> Path:
    from config import DATA_DIR

    name = f"{_hash_uid(uid)}_{module}.json"
    return DATA_DIR / "module_state" / name


def _load(uid: str, module: str) -> dict:
    key = f"{uid}:{module}"
    hit = _cache.get(key)
    if hit is not None:
        return hit
    path = state_path(uid, module)
    data: dict = {}
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data = raw
    except Exception:
        # Битый JSON состояния не должен ронять команду модуля:
        # максимум — потерять настройки.
        logger.warning("module_state: unreadable %s, starting empty", path)
        data = {}
    _cache[key] = data
    return data


def _save(uid: str, module: str) -> None:
    path = state_path(uid, module)
    data = _cache.get(f"{uid}:{module}") or {}
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
    except Exception:
        logger.exception("module_state: write failed %s", path)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


class ModuleState:
    """`ctx.state` — состояние модуля конкретного юзера.

    Экземпляр создаётся на каждый вызов хендлера и работает с кешем в
    памяти; на диск пишется сразу при `set`/`delete`/`clear`.
    """

    __slots__ = ("_uid", "_module")

    def __init__(self, uid: str, module: str) -> None:
        self._uid = str(uid)
        self._module = str(module)

    def get(self, key: str, default: Any = None) -> Any:
        """Значение по ключу. Отсутствующий ключ → `default`."""
        return _load(self._uid, self._module).get(str(key), default)

    def set(self, key: str, value: Any) -> None:
        """Записать значение (JSON-сериализуемое)."""
        _load(self._uid, self._module)[str(key)] = value
        _save(self._uid, self._module)

    def update(self, values: dict) -> None:
        """Записать несколько значений одним вызовом."""
        _load(self._uid, self._module).update(values or {})
        _save(self._uid, self._module)

    def delete(self, *keys: str) -> None:
        data = _load(self._uid, self._module)
        for key in keys:
            data.pop(str(key), None)
        _save(self._uid, self._module)

    def all(self) -> dict:
        """Копия всего состояния."""
        return dict(_load(self._uid, self._module))

    def clear(self) -> None:
        """Стереть состояние модуля для этого юзера."""
        _cache[f"{self._uid}:{self._module}"] = {}
        _save(self._uid, self._module)

    def __repr__(self) -> str:  # pragma: no cover - для логов
        return f"<ModuleState {self._uid}/{self._module}>"


def forget(uid: str, module: str) -> None:
    """Сбросить кеш (после удаления модуля)."""
    _cache.pop(f"{str(uid)}:{str(module)}", None)


__all__ = ["ModuleState", "forget", "state_path"]