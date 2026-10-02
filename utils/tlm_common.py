"""Single source of truth for three operations that were copy-pasted.

1. ``thread_id_of`` — извлечение id топика форума. Было в трёх копиях
   (``telethon_manager``, ``ai.py``, ``knowledge_collector``), причём две
   были байт-в-байт идентичны, а третья отличалась порядком проверок.
2. ``chat_link`` — построение ссылки на чат/юзера. Было в `watch.py`.
3. ``usernames_of`` — активные usernames сущности. Было в двух копиях
   дословно (``telethon_manager`` и ``inline/profile``).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 1. thread id
# ---------------------------------------------------------------------------


def _raw_message(obj: Any):
    """Внутреннее сообщение Telethon.

    Принимаем и сам `Message`, и `events.NewMessage.Event` (у события нужное
    лежит в `.message`). Раньше из-за этой разницы существовали две копии
    функции: одна брала ``obj.message.reply_to``, другая — ``obj.reply_to``.

    Выбираем по наличию ``reply_to``: у настоящего ``Message`` атрибут
    есть всегда (иногда ``None``), а у тестовых/обёрточных объектов поле
    ``message`` может оказаться чем-то иным.
    """
    inner = getattr(obj, "message", None)
    if inner is not None and hasattr(inner, "reply_to"):
        return inner
    if hasattr(obj, "reply_to"):
        return obj
    return inner if inner is not None else obj


def thread_id_of(message: Any) -> int:
    """Id топика форума. 0 = не форум / нет reply.

    Логика (один порядок на всех): ``reply_to_top_id`` — корень топика,
    он и держит сообщение в нужном топике; ``forum_topic`` +
    ``reply_to_msg_id`` — запасной путь, когда top_id не проставлен.
    """
    reply_to = getattr(_raw_message(message), "reply_to", None)
    if reply_to is None:
        return 0
    top = getattr(reply_to, "reply_to_top_id", None)
    if top:
        return int(top)
    if getattr(reply_to, "forum_topic", False):
        root = getattr(reply_to, "reply_to_msg_id", None)
        return int(root) if root else 0
    return 0


def telethon_reply_to(message: Any):
    """Id сообщения для ``reply_to``, чтобы ответ остался в том же топике."""
    reply_to = getattr(_raw_message(message), "reply_to", None)
    if reply_to is None:
        return None
    top = getattr(reply_to, "reply_to_top_id", None)
    if top:
        return int(top)
    root = getattr(reply_to, "reply_to_msg_id", None)
    return int(root) if root else None


# ---------------------------------------------------------------------------
# 2. ссылки на чаты
# ---------------------------------------------------------------------------


def chat_link(chat_id: int, entity: Any = None, *, me_id: int = 0) -> str:
    """Публичная ссылка на чат/юзера.

    Используется `.watched` (кликабельный список) и встроенными кнопками
    бота. ``entity`` ускоряет выбор ветки, но не обязателен.
    """
    if entity is not None:
        username = getattr(entity, "username", None)
        if username:
            if int(getattr(entity, "id", 0) or 0) == int(me_id or 0):
                return "https://t.me/SavedMessages"
            return f"https://t.me/{username}"
        if getattr(entity, "title", None):
            raw = int(getattr(entity, "id", chat_id))
        else:
            raw = int(chat_id)
    else:
        raw = int(chat_id)

    if raw < 0:
        digits = str(abs(raw))
        if digits.startswith("100"):
            digits = digits[3:]
        return f"https://t.me/c/{digits}"
    return f"tg://user?id={raw}"


# ---------------------------------------------------------------------------
# 3. usernames
# ---------------------------------------------------------------------------


def usernames_of(entity: Any, full_user: Any = None) -> list[str]:
    """Активные usernames сущности: collectibles (Premium) + legacy.

    Приоритет — ``full_user.usernames`` (новый формат слоя ~145), затем
    legacy ``entity.username``, если он не дублирует collectible.
    """
    out: list[str] = []
    if full_user is not None:
        for item in getattr(full_user, "usernames", None) or []:
            if getattr(item, "active", False):
                name = getattr(item, "username", None)
                if name and name not in out:
                    out.append(name)
    legacy = getattr(entity, "username", None)
    if legacy and legacy not in out:
        out.append(legacy)
    return out


def render_usernames(names: list[str]) -> str:
    """``Username: @a`` (один) или ``Usernames: @a @b`` (несколько)."""
    if not names:
        return ""
    if len(names) == 1:
        return f"Username: @{names[0]}"
    return "Usernames: " + " ".join(f"@{n}" for n in names)


__all__ = [
    "thread_id_of",
    "telethon_reply_to",
    "chat_link",
    "usernames_of",
    "render_usernames",
]
