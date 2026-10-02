import asyncio
import io
import logging
import os
import time

from aiogram.types import FSInputFile
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from utils.timezones import is_reset_value


def _ping_pairs(fields: dict):
    """Пары (подпись, значение) для карточки `.ping` — общий формат."""
    labels = (
        ("Chat ID", "chat_id"), ("User ID", "user_id"), ("DC", "dc_id"),
        ("Connected", "connected"), ("Authorized", "authorized"),
        ("Telegram API RTT", "api_rtt_ms"), ("get_me RTT", "get_me_rtt_ms"),
        ("Edit RTT", "edit_rtt_ms"),
    )
    out = []
    for label, key in labels:
        value = fields.get(key)
        if value is None:
            continue
        if key in ("connected", "authorized"):
            value = "yes" if value else "no"
        out.append((label, value))
    return out
from telethon.tl.types import MessageMediaDocument
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import User as TUser

import re

from config import (
    API_ID,
    API_HASH,
    SESSIONS_DIR,
    TEMP_DIR,
    AUTH_STATE_TTL,
    AUTH_CLEAN_INTERVAL,
    AUTO_TR_DELAY,
    DOT_TARGET_LIMIT,
    TELETHON_RESOLVE_TIMEOUT,
    TELETHON_SEND_TIMEOUT,
    NYA_EDIT_DELAY,
    TELETHON_RECONNECT_MAX_DELAY,
    TELETHON_HEALTH_INTERVAL,
    TELETHON_HEALTH_TIMEOUT,
    MAX_ACTIVE_SESSIONS,
    SESSION_START_BATCH,
    SESSION_START_DELAY,
)
from utils.storage import (
    user_sessions,
    save_user_sessions,
    was_processed,
    session_path,
    is_photo_allowed,
    is_chat_watched,
    is_auto_tr_chat,
    get_auto_tr_chats,
    get_auto_tr_language,
    toggle_auto_tr_chat,
    is_nya_chat,
    get_knowledge_selected_chats,
)
from utils.texts import Texts, render_for_user
from utils.tlm_common import (
    render_usernames as _render_usernames,
    thread_id_of,
    telethon_reply_to,
    usernames_of as _usernames_of,
)
from utils import rate_limit_gate as gate
from utils.escape import esc as _esc
from utils.premium import invalidate_premium_cache

logger = logging.getLogger(__name__)

# Алиасы команд берём из единого реестра utils.cmds (листовой модуль без
# импортов проекта — его можно тянуть отсюда без циклической зависимости).
# Раньше в этом файле был свой хардкод-кортеж для `.шаб` плюс девять других
# списков в самих модулях команд; дрейф ловили лишь два теста, и `.пер`
# реально разошёлся с `_helpdb`. Теперь источник ровно один.
from utils.cmds import (  # noqa: E402
    DEL_CMDS,
    DM_CMDS,
    TAGALL_CMDS,
    ADMINS_CMDS,
    INVITE_CMDS,
    PIN_CMDS,
    UNPIN_CMDS,
    VGF_CMDS,
    NYA_CMDS,
    QUOTE_CMDS,
    GOVNO_CMDS,
    TEMPLATE_CMDS,
    PING_CMDS,
    TIME_CMDS,
    ID_CMDS,
    ME_CMDS,
    CHAT_CMDS,
    WHO_CMDS,
    LOVE_CMDS,
    HELP_CMDS,
    COIN_CMDS,
    WATCH_CMDS,
    UNWATCH_CMDS,
    WATCHED_CMDS,
    NET_CMDS,
    TR_CMDS,
    CALC_CMDS,
    SAVE_CMDS,
    HASH_CMDS,
    UUID_CMDS,
    B64_CMDS,
    TIMEZONE_CMDS,
    AI_CMDS,
    OPENCODE_CMDS,
)


def _command_card(title: str, text: str) -> str:
    """Apply the shared command layout without creating an import cycle."""
    from handlers.commands._base import command_card
    return command_card(title, text)


async def _topic_name_async(client, chat_id: int, thread_id: int) -> str:
    """Пытается получить имя топика по thread_id через Telethon.

    Использует GetForumTopicsByIDRequest (точечный запрос по id, без
    пагинации) — с проверкой id в ответе на случай рассинхрона.
    """
    if not thread_id or client is None:
        return ""
    try:
        from telethon.tl.functions.messages import GetForumTopicsByIDRequest
        res = await client(GetForumTopicsByIDRequest(peer=chat_id, topics=[thread_id]))
        if res and getattr(res, "topics", None):
            for t in res.topics:
                if getattr(t, "id", None) == thread_id:
                    return getattr(t, "title", None) or ""
    except Exception:
        pass
    return ""


auth_states: dict[str, dict] = {}
# AUTH_STATE_TTL — в config.py (default 300с / 5 мин).


async def cleanup_auth_states():
    """Чистка зависших auth_states (юзер начал ввод и ушёл)."""
    now = time.time()
    expired = []
    for uid, st in list(auth_states.items()):
        ts = st.get("_ts")
        if ts and now - ts > AUTH_STATE_TTL:
            expired.append(uid)
    for uid in expired:
        st = auth_states.pop(uid, None)
        if st and "client" in st:
            try:
                await st["client"].disconnect()
            except Exception:
                pass
        logger.info(f"auth_states: cleaned expired uid={uid}")


async def auth_state_cleaner():
    while True:
        try:
            await asyncio.sleep(AUTH_CLEAN_INTERVAL)
            await cleanup_auth_states()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"auth_state_cleaner error: {e}")


def cleanup_orphan_sessions():
    """Удаляет .session/-journal файлы, для которых нет активной записи в user_sessions."""
    import hashlib
    valid_hashes = set()
    for uid in user_sessions.keys():
        h = hashlib.sha256(uid.encode()).hexdigest()[:16]
        valid_hashes.add(h)
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        for f in SESSIONS_DIR.iterdir():
            name = f.name
            if not (name.endswith(".session") or name.endswith(".session-journal")):
                continue
            stem = name.split(".")[0]
            if stem in valid_hashes:
                continue
            try:
                f.unlink()
                logger.info(f"cleanup_orphan_sessions: removed {name}")
            except OSError as e:
                logger.warning(f"cleanup_orphan_sessions: cannot remove {name}: {e}")
    except Exception as e:
        logger.warning(f"cleanup_orphan_sessions failed: {e}")


class _TooBigDownload(Exception):
    """Маркер: файл превысил лимит хеширования (бросается из progress_callback)."""


HASH_DOWNLOAD_MAX_BYTES = 25 * 1024 * 1024


def _abort_big_download(received: int, total: int) -> None:
    """progress_callback для Telethon-download: аборт до OOM (2 ГБ видео в RAM)."""
    if received > HASH_DOWNLOAD_MAX_BYTES:
        raise _TooBigDownload()


class TelethonManager:
    def __init__(self):
        self._clients: dict[str, TelegramClient] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._bot = None
        # Per-(user, chat) lock для сериализации auto-tr replies (защита от flood control).
        self._tr_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._auto_tr_messages: set[tuple[str, int, int]] = set()
        # Когда ключ был добавлен — для свипа зависших (см. sweep_auto_tr_messages).
        self._auto_tr_seen: dict[tuple[str, int, int], float] = {}
        # Per-(user, chat) lock для сериализации .ня edit'ов (та же защита:
        # если юзер бёрстит 5+ сообщениями подряд — N concurrent event.edit
        # → FloodWait. С lock'ом edit'ы идут последовательно).
        self._nya_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._start_locks: dict[str, asyncio.Lock] = {}
        self._stopping: set[str] = set()
        self._health_task: asyncio.Task | None = None
        # Отдельно от keep-alive: сбор истории должен отменяться до disconnect.
        from utils.knowledge_collector import knowledge_collector
        self._knowledge_collector = knowledge_collector

    def set_bot(self, bot):
        self._bot = bot

    async def start_client(self, user_id: str) -> bool:
        user_id = str(user_id)
        if user_id in self._clients:
            return True
        lock = self._start_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            if user_id in self._clients:
                return True
            self._stopping.discard(user_id)
            return await self._start_client_once(user_id)

    async def _start_client_once(self, user_id: str) -> bool:
        sp = session_path(user_id)
        client = TelegramClient(sp, API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"Telethon session for {user_id} not authorized")
                await client.disconnect()
                return False
        except Exception as e:
            logger.warning(f"Telethon connect failed for {user_id}: {e}")
            try:
                await client.disconnect()
            except Exception:
                pass
            return False

        self._clients[user_id] = client

        @client.on(events.NewMessage(outgoing=True))
        async def outgoing_handler(event):
            try:
                await asyncio.wait_for(
                    self._handle_outgoing(user_id, event), timeout=300
                )
            except asyncio.TimeoutError:
                logger.warning(f"outgoing handler timeout for {user_id} msg={event.id}")
                try:
                    await event.edit(
                        _command_card("Command", "[x] Таймаут обработки — попробуй ещё раз."),
                        parse_mode="html",
                    )
                except Exception:
                    pass
            except FloodWaitError as e:
                wait = getattr(e, "seconds", 0) or 0
                logger.warning(f"outgoing handler FloodWait {wait}s for {user_id}")
                try:
                    await event.edit(
                        _command_card("Command", f"[x] FloodWait {wait}с — подожди и повтори."),
                        parse_mode="html",
                    )
                except Exception:
                    pass
            except Exception:
                logger.exception(f"outgoing handler failed for {user_id} msg={event.id}")

        @client.on(events.NewMessage())
        async def incoming_handler(event):
            try:
                await asyncio.wait_for(
                    self._handle_incoming(user_id, event), timeout=300
                )
            except asyncio.TimeoutError:
                logger.warning(f"incoming handler timeout for {user_id} msg={event.id}")
            except Exception:
                logger.exception(f"incoming handler failed for {user_id} msg={event.id}")

        task = asyncio.create_task(self._keep_alive(user_id, client))
        self._tasks[user_id] = task
        logger.info(f"Telethon client started for user {user_id}")
        return True

    async def _keep_alive(self, user_id: str, client: TelegramClient):
        reconnect_delay = 1
        try:
            while user_id not in self._stopping:
                await client.run_until_disconnected()
                if user_id in self._stopping:
                    break
                logger.warning("Telethon client %s disconnected; reconnect in %ss", user_id, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                try:
                    await client.connect()
                    if await client.is_user_authorized():
                        reconnect_delay = 1
                        continue
                    logger.warning("Telethon client %s is no longer authorized", user_id)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("Telethon reconnect failed for %s: %s", user_id, e)
                    reconnect_delay = min(reconnect_delay * 2, TELETHON_RECONNECT_MAX_DELAY)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Telethon client {user_id} disconnected: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception as e:
                logger.warning(f"keep_alive: disconnect failed for {user_id}: {e}")
            self._clients.pop(user_id, None)
            self._tasks.pop(user_id, None)

    def get_client(self, user_id: str) -> TelegramClient | None:
        return self._clients.get(user_id)

    def sweep_auto_tr_messages(self, max_age: float = 300.0) -> int:
        """Вычистить зависшие ключи `_auto_tr_messages`.

        Ключ снимается в `finally` задачи, но задача может быть отменена
        ДО первого шага (create_task + немедленный cancel на shutdown) — тогда
        `finally` не отработает и ключ утечёт навсегда. Свип по возрасту
        закрывает эту дыру.

        Возвращает число удалённых ключей.
        """
        now = time.monotonic()
        stale = [k for k, seen in self._auto_tr_seen.items() if now - seen > max_age]
        for key in stale:
            self._auto_tr_seen.pop(key, None)
            self._auto_tr_messages.discard(key)
        if stale:
            logger.info("sweep_auto_tr_messages: dropped %d stale keys", len(stale))
        return len(stale)

    async def stop_client(self, user_id: str):
        user_id = str(user_id)
        self._stopping.add(user_id)
        await self._knowledge_collector.stop(user_id)
        task = self._tasks.get(user_id)
        if task and task is not asyncio.current_task():
            task.cancel()
        client = self._clients.pop(user_id, None)
        self._tasks.pop(user_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception as e:
                logger.warning(f"stop_client: disconnect failed for {user_id}: {e}")
        if task and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        for key in [key for key in self._tr_locks if key[0] == user_id]:
            self._tr_locks.pop(key, None)
        for key in [key for key in self._nya_locks if key[0] == user_id]:
            self._nya_locks.pop(key, None)
        for key in [key for key in self._auto_tr_messages if key[0] == user_id]:
            self._auto_tr_messages.discard(key)
            self._auto_tr_seen.pop(key, None)
        # Старт-локи тоже росли без границ (создаются в start_client).
        self._start_locks.pop(user_id, None)
        self._stopping.discard(user_id)

    async def stop_all(self):
        for uid in list(self._clients.keys()):
            await self.stop_client(uid)
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def start_health_check(self) -> None:
        """Start one manager-owned liveness task for all active clients."""
        if self._health_task and not self._health_task.done():
            return
        self._health_task = asyncio.create_task(self._health_check_loop())

    async def stop_health_check(self) -> None:
        task = self._health_task
        self._health_task = None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _health_check_loop(self):
        while True:
            try:
                await asyncio.sleep(TELETHON_HEALTH_INTERVAL)
                await self.check_clients_health()
            except asyncio.CancelledError:
                return
            except Exception:
                # Петля обязана жить дальше: один брошенный RPC не должен
                # убивать health-check для всех клиентов.
                logger.exception("Telethon health-check iteration failed (continuing)")

    async def check_clients_health(self) -> dict[str, bool]:
        """Check each client and kick the existing reconnect loop if needed."""
        results: dict[str, bool] = {}
        self.sweep_auto_tr_messages()
        for uid, client in list(self._clients.items()):
            if uid in self._stopping:
                continue
            healthy = False
            try:
                if client.is_connected():
                    await asyncio.wait_for(client.get_me(), timeout=TELETHON_HEALTH_TIMEOUT)
                    healthy = True
                else:
                    # _keep_alive owns reconnects. Avoid a second concurrent
                    # connect() here; a disconnected client will be retried
                    # by that task with exponential backoff.
                    logger.warning("Telethon client %s is disconnected; reconnect is pending", uid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telethon health-check failed for %s: %s", uid, exc)
                try:
                    await client.disconnect()
                except Exception:
                    logger.debug("Health-check disconnect failed for %s", uid, exc_info=True)
            results[uid] = healthy
        return results

    async def logout(self, user_id: str) -> bool:
        """Полный logout: revoke сессии на стороне Telegram, остановка клиента, удаление .session, очистка user_sessions."""
        sp = session_path(user_id)
        await self._knowledge_collector.stop(user_id)
        session_file = sp + ".session"
        journal_file = sp + ".session-journal"
        revoked = False

        client = self._clients.get(user_id)
        if client:
            self._stopping.add(user_id)
            task = self._tasks.get(user_id)
            if task and task is not asyncio.current_task():
                task.cancel()
            try:
                await client.log_out()
                revoked = True
            except Exception as e:
                logger.warning(f"logout: log_out failed for {user_id}: {e}")
            try:
                await client.disconnect()
            except Exception:
                pass
            self._clients.pop(user_id, None)
            self._tasks.pop(user_id, None)
            if task and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            self._stopping.discard(user_id)
        else:
            tmp = TelegramClient(sp, API_ID, API_HASH)
            try:
                await tmp.connect()
                if await tmp.is_user_authorized():
                    await tmp.log_out()
                    revoked = True
                else:
                    logger.info(f"logout: uid={user_id} session not authorized, nothing to revoke")
            except Exception as e:
                logger.warning(f"logout: standalone log_out failed for {user_id}: {e}")
            finally:
                try:
                    await tmp.disconnect()
                except Exception:
                    pass

        for p in (session_file, journal_file):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError as e:
                logger.warning(f"logout: cannot remove {p}: {e}")

        if user_id in user_sessions:
            user_sessions.pop(user_id, None)
            save_user_sessions()

        invalidate_premium_cache(user_id)
        from utils.rate_limit import clear as _rate_clear
        _rate_clear(user_id)

        # Чистим per-(user, chat) asyncio.Lock'и чтобы не накапливались.
        for k in [k for k in self._tr_locks if k[0] == user_id]:
            self._tr_locks.pop(k, None)
        for k in [k for k in self._nya_locks if k[0] == user_id]:
            self._nya_locks.pop(k, None)
        for k in [k for k in self._auto_tr_messages if k[0] == user_id]:
            self._auto_tr_messages.discard(k)
            self._auto_tr_seen.pop(k, None)
        # clear_owner бьёт по общей базе (DELETE по 200k+ строк). Уводим в
        # поток: раньше это было ~10 с блокировки event loop на КАЖДЫЙ logout,
        # и все остальные пользователи стояли колом.
        from utils.knowledge_db import clear_owner
        await asyncio.to_thread(clear_owner, user_id)

        logger.info(f"logout: done for {user_id} revoked={revoked}")
        # Раньше здесь был безусловный `return True`, из-за чего UI всегда
        # показывал «сессия отозвана в Telegram» даже когда log_out() упал,
        # а Texts.Logout.NOTE_LOCAL был недостижим.
        return revoked

    async def start_all_active(self):
        """Поднять все сохранённые активные сессии.

        Раньше все клиенты поднимались ОДНИМ `asyncio.gather` без ограничений:
        200 сохранённых сессий = 200 одновременных connect() + is_user_authorized()
        на старте, что почти гарантированно ловит flood-control по IP/API_ID
        (все аккаунты делят одни и те же креды). Теперь:

        * `MAX_ACTIVE_SESSIONS` жёстко ограничивает количество (0 = без лимита);
        * подъём идёт волнами по `SESSION_START_BATCH`, а не разом.
        """
        active_uids = [
            str(uid) for uid, data in user_sessions.items()
            if isinstance(data, dict) and data.get("status") == "active"
        ]
        if not active_uids:
            logger.info("start_all_active: no active sessions to restore")
            return

        if MAX_ACTIVE_SESSIONS and len(active_uids) > MAX_ACTIVE_SESSIONS:
            logger.warning(
                "start_all_active: %d stored sessions but MAX_ACTIVE_SESSIONS=%d — "
                "поднимутся первые %d, остальные ждут ручного подключения",
                len(active_uids), MAX_ACTIVE_SESSIONS, MAX_ACTIVE_SESSIONS,
            )
            active_uids = active_uids[:MAX_ACTIVE_SESSIONS]

        for start in range(0, len(active_uids), SESSION_START_BATCH):
            batch = active_uids[start:start + SESSION_START_BATCH]
            if start:
                # Пауза между волнами, чтобы Telegram не увидел залп хендшейков.
                await asyncio.sleep(SESSION_START_DELAY)
            # Restore clients independently. One broken/stale session must not
            # prevent the remaining accounts in the batch from coming online.
            results = await asyncio.gather(
                *(self._restore_client(uid) for uid in batch),
                return_exceptions=True,
            )
            for uid, result in zip(batch, results):
                if isinstance(result, BaseException):
                    logger.error("start_all_active: uid=%s failed during restore: %s", uid, result)
                else:
                    logger.info("start_all_active: uid=%s started=%s", uid, result)

    async def _restore_client(self, uid: str) -> bool:
        """Restore one persisted client and resume its durable work."""
        try:
            ok = await self.start_client(uid)
            if ok:
                await self._knowledge_collector.resume_pending(uid, self._clients[uid])
            else:
                logger.warning(
                    "start_all_active: session for uid=%s was not authorized or unavailable",
                    uid,
                )
            return ok
        except Exception:
            logger.exception("start_all_active: uid=%s restore failed", uid)
            return False

    async def resolve_entity(self, user_id: str, target: str):
        client = self.get_client(user_id)
        if not client:
            return None
        try:
            return await asyncio.wait_for(
                client.get_entity(target),
                timeout=TELETHON_RESOLVE_TIMEOUT,
            )
        except Exception as e:
            logger.warning(f"Resolve {target} for {user_id} failed: {e}")
            return None

    async def download_view_once(
        self, user_id: str, chat_id: int, msg_id: int, thread_id: int = 0
    ) -> str | None:
        client = self.get_client(user_id)
        if not client:
            return None
        try:
            # get_messages по ids не зависит от топика — Telethon получает
            # сообщение напрямую. thread_id тут только для логов/уника.
            msg = await asyncio.wait_for(
                client.get_messages(chat_id, ids=msg_id),
                timeout=TELETHON_RESOLVE_TIMEOUT,
            )
            if not msg or not getattr(msg, "media", None):
                return None
            TEMP_DIR.mkdir(parents=True, exist_ok=True)
            ext = ".jpg"
            if isinstance(msg.media, MessageMediaDocument):
                doc = msg.media.document
                mime = (getattr(doc, "mime_type", None) or "")
                if "video" in mime:
                    ext = ".mp4"
                elif "gif" in mime:
                    ext = ".gif"
            path = str(TEMP_DIR / f"{user_id}_{chat_id}_{msg_id}_{int(time.time())}{ext}")
            try:
                await asyncio.wait_for(
                    client.download_media(msg, file=path),
                    timeout=90,
                )
            except Exception:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
                raise
            return path
        except Exception as e:
            logger.error(f"Download view-once for {user_id} failed: {e}")
            return None

    async def download_avatar_bytes(
        self, user_id: str, entity,
    ) -> tuple[str, bytes] | None:
        """Скачивает аватарку entity в виде bytes (без записи на диск).

        Возвращает кортеж `(filename, bytes)` с правильным расширением
        (jpg/png/gif/webp, определяется по magic bytes через `_detect_avatar_ext`).
        Это критично для Telethon send_file: если передавать просто bytes,
        Telegram получает "unnamed" без MIME и не рендерит превью.

        Возвращает None если аватарки нет / юзер удалён / MTProto ошибка.
        """
        client = self.get_client(user_id)
        if not client:
            return None
        if getattr(entity, "deleted", False):
            return None
        try:
            data = await client.download_profile_photo(entity, file=bytes)
        except Exception as e:
            logger.warning(f"download_avatar_bytes for {user_id} failed: {e}")
            return None
        if not data:
            return None
        return (_detect_avatar_ext(data), data)

    async def _who_with_reply(self, user_id: str, event, sender) -> None:
        """.who reply case: показать карточку отправителя.

        Стратегия (race-free):
        1. Пробуем send_file/send_message (с timeout 30с) с reply_to на
           исходное сообщение которое reply'ил юзер.
        2. Если send удался — best-effort `event.delete()` чтобы убрать
           исходный `.who` (если не получится — UX терпимый, дубль).
        3. На любой flop — fallback edit'ом текста на месте `.who`.

        Раньше всегда делал `event.delete()` ПЕРЕД send_file — это и было
        race condition, если delete падал, в чате оба сообщения.
        """
        client = self.get_client(user_id)
        full_user = None
        if isinstance(sender, TUser) and client:
            try:
                full_user = await asyncio.wait_for(
                    client(GetFullUserRequest(sender.id)),                        timeout=TELETHON_RESOLVE_TIMEOUT,
                )
            except Exception:
                full_user = None

        text = _format_who_telethon(sender, full_user)
        photo = await self.download_avatar_bytes(user_id, sender)
        rto = event.reply_to_msg_id

        if client:
            try:
                if photo:
                    filename, photo_data = photo
                    # Telethon send_file ломается на tuple (name, bytes) —
                    # bytes не имеет .read(). Используем io.BytesIO с .name
                    # (Telethon использует .name для инференса MIME по расширению).
                    photo_buf = io.BytesIO(photo_data)
                    photo_buf.name = filename
                    await asyncio.wait_for(
                        client.send_file(
                            event.chat_id,
                            file=photo_buf,
                            caption=text,
                            reply_to=rto,
                            parse_mode="html",
                        ),
                        timeout=TELETHON_SEND_TIMEOUT,
                    )
                else:
                    await asyncio.wait_for(
                        client.send_message(
                            event.chat_id,
                            text,
                            reply_to=rto,
                            parse_mode="html",
                        ),
                        timeout=TELETHON_SEND_TIMEOUT,
                    )
                try:
                    await event.delete()
                except Exception:
                    pass
                return
            except asyncio.TimeoutError:
                logger.warning(f"_who_with_reply timeout for {user_id}")
                # Команда существует — короткий notice безопасен (полный текст
                # не дублируем: Telethon мог частично отправить результат).
                try:
                    await event.edit(
                        _command_card("Who", "[x] Таймаут — если результат отправился, он уже в чате."),
                        parse_mode="html",
                    )
                except Exception:
                    pass
                return
            except FloodWaitError as e:
                logger.warning(f"_who_with_reply FloodWait {e.seconds}s for {user_id}")
                try:
                    await event.edit(
                        _command_card("Who", f"[x] FloodWait {e.seconds}с"),
                        parse_mode="html",
                    )
                except Exception:
                    pass
                return
            except Exception as e:
                logger.warning(f"_who_with_reply failed for {user_id}: {e}")

        # Fallback: edit original to text.
        try:
            await event.edit(_command_card("Who", text), parse_mode="html")
        except Exception:
            pass

    async def _who_with_args(self, user_id: str, event, args: str) -> None:
        """.who @user1 @user2 ... — мульти-targets.

        Edit оригинального сообщения → progress. Для каждого target резолв
        через Telethon; если есть фото — присылаем photo+caption в чат
        (event.client.send_file), иначе текст. В конце edit на summary.
        """
        client = self.get_client(user_id)
        if not client:
            await event.edit(
                _command_card("Who", "[x] Telethon-клиент не активен для этого юзера."),
                parse_mode="html",
            )
            return

        targets = parse_dot_targets(args)
        if not targets:
            await event.edit(
                _command_card("Who", "[?] Использование: <code>.who @user1 @user2 ...</code>"),
                parse_mode="html",
            )
            return

        try:
            await event.edit(
                _command_card("Who", f"Резолвлю {len(targets)} профилей..."),
                parse_mode="html",
            )
        except Exception:
            pass

        resolved = 0
        failed: list[tuple[str, str]] = []
        for target in targets:
            entity = None
            err: str | None = None
            try:
                entity = await asyncio.wait_for(
                    client.get_entity(target),                        timeout=TELETHON_RESOLVE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                err = "таймаут 10с"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"

            if entity is None:
                failed.append((target, err or "неизвестная ошибка"))
                continue

            full_user = None
            if isinstance(entity, TUser):
                try:
                    full_user = await asyncio.wait_for(
                        client(GetFullUserRequest(entity.id)),                        timeout=TELETHON_RESOLVE_TIMEOUT,
                    )
                except Exception:
                    full_user = None

            text = _format_who_telethon(entity, full_user)
            photo = await self.download_avatar_bytes(user_id, entity)
            rto = telethon_reply_to(event)

            try:
                if photo:
                    filename, photo_data = photo
                    # Telethon send_file не принимает tuple (name, bytes) —
                    # bytes не имеет .read(). Используем io.BytesIO с .name
                    # для корректного инференса MIME по расширению.
                    photo_buf = io.BytesIO(photo_data)
                    photo_buf.name = filename
                    await asyncio.wait_for(
                        client.send_file(
                            event.chat_id,
                            file=photo_buf,
                            caption=text,
                            reply_to=rto,
                            parse_mode="html",
                        ),
                        timeout=TELETHON_SEND_TIMEOUT,
                    )
                else:
                    await asyncio.wait_for(
                        client.send_message(
                            event.chat_id,
                            text,
                            reply_to=rto,
                            parse_mode="html",
                        ),
                        timeout=TELETHON_SEND_TIMEOUT,
                    )
                resolved += 1
            except asyncio.TimeoutError:
                failed.append((target, f"send timeout 30с"))
            except FloodWaitError as e:
                failed.append((target, f"FloodWait {e.seconds}с"))
            except Exception as e:
                failed.append((target, f"send error: {e}"))
            if target is not targets[-1]:
                # Пауза между отправками — снижаем риск FloodWait на сериях.
                await asyncio.sleep(1)

        summary_parts = [
            f"Резолвлено <code>{resolved}</code> из <code>{len(targets)}</code>"
        ]
        if failed:
            summary_parts.append(f"<i>Не найдено: {len(failed)}</i>")
            for t, e in failed[:3]:
                summary_parts.append(f"<code>{_esc(t)}</code> — <code>{_esc(e)}</code>")
            if len(failed) > 3:
                summary_parts.append(f"<i>…и ещё {len(failed) - 3}</i>")
        try:
            await event.edit(
                _command_card("Who", "\n".join(summary_parts)),
                parse_mode="html",
            )
        except Exception as e:
            logger.warning(f"_who_with_args summary edit failed: {e}")

    async def _handle_ping(self, user_id: str, event) -> None:
        """Render a detailed, latency-aware ping card for the user session."""
        started = time.monotonic()
        client = self.get_client(user_id)
        connected = bool(client and client.is_connected())
        authorized = False
        me = None
        api_rtt = None
        get_me_rtt = None
        edit_rtt = None

        async def timed(call):
            begin = time.monotonic()
            try:
                value = await asyncio.wait_for(call(), timeout=TELETHON_RESOLVE_TIMEOUT)
                return value, int((time.monotonic() - begin) * 1000)
            except Exception:
                return None, None

        if client and connected:
            try:
                authorized = bool(await asyncio.wait_for(
                    client.is_user_authorized(), timeout=TELETHON_RESOLVE_TIMEOUT,
                ))
            except Exception:
                authorized = False
            if authorized:
                # A separate lightweight RPC gives a Telegram API RTT distinct
                # from the cached/local session status check.
                _, api_rtt = await timed(lambda: client.get_dialogs(limit=1))
                me, get_me_rtt = await timed(client.get_me)

        # Measure an actual Telegram edit before rendering the final card.
        edit_started = time.monotonic()
        try:
            await asyncio.wait_for(event.edit("<i>Измеряю задержку…</i>", parse_mode="html"), timeout=TELETHON_RESOLVE_TIMEOUT)
            edit_rtt = int((time.monotonic() - edit_started) * 1000)
        except Exception:
            edit_rtt = None

        from utils.shared_cmd import ping_fields

        user_id_value = getattr(me, "id", user_id) if me else user_id
        dc_id = getattr(getattr(client, "session", None), "dc_id", None) if client else None
        fields = ping_fields(
            api_rtt_ms=api_rtt, get_me_rtt_ms=get_me_rtt, edit_rtt_ms=edit_rtt,
            chat_id=event.chat_id, user_id=user_id_value, dc_id=dc_id,
            connected=connected, authorized=authorized,
        )
        body = "\n".join(f"<b>{label}:</b> <code>{value}</code>"
                          for label, value in _ping_pairs(fields))
        try:
            await asyncio.wait_for(
                event.edit(_command_card("Ping", body), parse_mode="html"),
                timeout=TELETHON_RESOLVE_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("Detailed ping final edit failed: %s", exc)

    async def _handle_outgoing(self, user_id: str, event: events.NewMessage.Event):
        if not event.raw_text:
            return
        # После первичного обхода сохраняем и новые исходящие. Saved Messages
        # collector пропускает по self_id, поэтому прогресс не попадёт в индекс.
        # Гейт по включённой фиче: без выбранных чатов не спавним задачу на
        # каждое сообщение (до этого index_live читал SQLite для КАЖДОГО юзера
        # на КАЖДОЕ сообщение даже при полностью выключенной базе знаний).
        if get_knowledge_selected_chats(user_id):
            asyncio.create_task(self._knowledge_collector.index_live(user_id, event))
        text = event.raw_text.strip()
        # .ня режим: если чат в catgirl-режиме, запускаем background-rewrite для
        # ОБЫЧНЫХ (не-dоt) сообщений. Сама команда `.ня` начинается с `.` и
        # обрабатывается ниже через elif — её rewrite не должен трогать
        # (иначе `.ня` → ``н-н-ня``, юзер запутается).
        if not text.startswith("."):
            try:
                if is_auto_tr_chat(str(user_id), event.chat_id):
                    # Ключ держим, пока задача жива, и снимаем в её `finally`.
                    # Раньше здесь стояло `if key in ...: discard; return` —
                    # это снимало guard у ещё работающей задачи, и третье
                    # событие спавнило вторую конкурентную задачу, чей
                    # except-ветка мог откатить перевод первой.
                    key = (str(user_id), event.chat_id, event.id)
                    if key in self._auto_tr_messages:
                        return
                    self._auto_tr_messages.add(key)
                    self._auto_tr_seen[key] = time.monotonic()
                    asyncio.create_task(
                        self._apply_auto_tr_outgoing(user_id, event, text),
                        name=f"auto-tr-out-{user_id}-{event.chat_id}-{event.id}",
                    )
                # Ветка .ня была недостижимой: первая делала `return`.
                # Обе фичи могут быть включены в одном чате, и обе должны
                # срабатывать (перевод применяется к тексту до/после — не важно,
                # главное не терять ни одну).
                if is_nya_chat(str(user_id), event.chat_id):
                    asyncio.create_task(
                        self._apply_nya(user_id, event, text),
                        name=f"nya-{user_id}-{event.chat_id}-{event.id}",
                    )
            except Exception:
                # Если что-то сломалось в check'е — не помашем основной поток.
                logger.debug("_handle_outgoing: auto-tr/nya dispatch failed", exc_info=True)
            return

        if event.is_private:
            chat = await event.get_chat()
            if getattr(chat, "bot", False):
                return

        chat_id = event.chat_id
        msg_id = event.id
        tid = thread_id_of(event)
        if was_processed(chat_id, msg_id, tid, user_id):
            return

        # reply_to для остаться в этом же топике при respond/send_message
        rto = telethon_reply_to(event)

        cmd_full = text.lower()
        head = cmd_full.split(maxsplit=1)[0]
        from handlers.commands import _base
        from handlers.commands._helpdb import is_help_request, render as render_help

        ok, key = is_help_request(text)
        if ok:
            await event.edit(render_help(key), parse_mode="html")
            return

        # Пользовательские модули. Проверка идёт ДО системной цепочки, иначе
        # перехватить системную команду (свой `.ping` вместо системного)
        # было бы невозможно и весь конфликт-механизм бессмыслен.
        #
        # Порядок именно такой:
        #   1. команда отключена юзером → тишина (и для системной, и для
        #      модульной: если модуль её перехватил — см. п.2);
        #   2. модуль заявил этот head → выполняем модуль;
        #   3. иначе — обычная системная цепочка ниже.
        if await self._handle_modules(user_id, event, head, tid):
            return

        # Единый гейт для обоих путей (utils.rate_limit_gate) — иначе
        # набор ограниренных команд расходился между aiogram и Telethon.
        if gate.is_limited(head) and not gate.check(head, user_id):
            await event.edit(_command_card("Command", gate.RATE_LIMIT_TEXT), parse_mode="html")
            return

        if head in PING_CMDS:
            await self._handle_ping(user_id, event)
        elif head in TIME_CMDS:
            await event.edit(await _base.render_time(user_id), parse_mode="html")
        elif head in ID_CMDS:
            sender = await event.get_sender()
            await event.edit(
                await _base.render_id(
                    user_id,
                    chat_id=event.chat_id,
                    user_id=sender.id if sender else 0,
                    username=getattr(sender, "username", None),
                    first_name=getattr(sender, "first_name", ""),
                    thread_id=tid or None,
                ),
                parse_mode="html",
            )
        elif head in LOVE_CMDS:
            await self._handle_anim(user_id, event, kind="love")
        elif head in GOVNO_CMDS:
            await self._handle_anim(user_id, event, kind="govno")
        elif head in HELP_CMDS:
            await event.edit(await _base.render_help(user_id, True), parse_mode="html")
        elif head in ME_CMDS:
            sender = await event.get_sender()
            full_self = None
            client_self = self.get_client(user_id)
            if isinstance(sender, TUser) and client_self:
                try:
                    full_self = await asyncio.wait_for(
                        client_self(GetFullUserRequest("me")),                        timeout=TELETHON_RESOLVE_TIMEOUT,
                    )
                except Exception:
                    full_self = None
            await event.edit(
                _format_me_telethon(sender, full_self),
                parse_mode="html",
            )
        elif head in CHAT_CMDS:
            chat = await event.get_chat()
            full_chat = None
            client_chat = self.get_client(user_id)
            if client_chat:
                try:
                    from telethon.tl.functions.channels import GetFullChannelRequest
                    full_chat = await asyncio.wait_for(
                        client_chat(GetFullChannelRequest(chat)),                        timeout=TELETHON_RESOLVE_TIMEOUT,
                    )
                except Exception:
                    full_chat = None
            await event.edit(
                _format_chat_telethon(chat, event.chat_id, full_chat),
                parse_mode="html",
            )
        elif head in WHO_CMDS:
            parts = event.raw_text.strip().split(maxsplit=1)
            args = parts[1].strip() if len(parts) > 1 else ""
            if args:
                await self._who_with_args(user_id, event, args)
            else:
                msg = await event.get_reply_message()
                if not msg or not msg.sender:
                    await event.edit(
                        _command_card("Who", "[x] Ответь на сообщение или укажи @username."),
                        parse_mode="html",
                    )
                    return
                await self._who_with_reply(user_id, event, msg.sender)
        elif head in WATCH_CMDS or head in UNWATCH_CMDS or head in WATCHED_CMDS:
            from handlers.commands.watch import handle_telethon
            await handle_telethon(user_id, event, thread_id=tid)
        elif head in NET_CMDS:
            from handlers.commands.netcmds import _do_net
            parts = event.raw_text.strip().split(maxsplit=1)
            args = parts[1] if len(parts) > 1 else ""
            reply = await event.get_reply_message()
            reply_text = reply.raw_text if reply else None
            await event.edit(await _do_net(user_id, args, reply_text), parse_mode="html", link_preview=False)
        elif head in DEL_CMDS:
            from handlers.commands.delmsg import handle as handle_del
            await handle_del(user_id, event)
        elif head in TR_CMDS:
            await self._handle_tr(user_id, event)
        elif head in CALC_CMDS:
            await self._handle_calc(event)
        elif head in SAVE_CMDS:
            await self._handle_save(event)
        elif head in HASH_CMDS:
            await self._handle_hash(event)
        elif head in UUID_CMDS:
            await self._handle_uuid(event)
        elif head in B64_CMDS:
            await self._handle_b64(event)
        elif head in TIMEZONE_CMDS:
            await self._handle_timezone(user_id, event)
        elif head in TAGALL_CMDS:
            from handlers.commands.tagall import handle as handle_tagall
            await handle_tagall(user_id, event)
        elif head in DM_CMDS:
            from handlers.commands.dm import handle as handle_dm
            await handle_dm(user_id, event)
        elif head in ADMINS_CMDS:
            from handlers.commands.admins import handle as handle_admins
            await handle_admins(user_id, event)
        elif head in PIN_CMDS:
            from handlers.commands.pin import handle as handle_pin
            await handle_pin(user_id, event)
        elif head in UNPIN_CMDS:
            from handlers.commands.pin import handle as handle_pin
            await handle_pin(user_id, event)
        elif head in INVITE_CMDS:
            from handlers.commands.invitelink import handle as handle_invite
            await handle_invite(user_id, event)
        elif head in QUOTE_CMDS:
            from handlers.commands.quote import handle as handle_quote
            await handle_quote(user_id, event)
        elif head in TEMPLATE_CMDS:
            from handlers.commands.template import handle as handle_template
            await handle_template(user_id, event)
        elif head in NYA_CMDS:
            from handlers.commands.nya import handle as handle_nya
            await handle_nya(user_id, event)
        elif head in VGF_CMDS:
            from handlers.commands.vgf import handle as handle_vgf
            await handle_vgf(user_id, event)
        elif head in COIN_CMDS:
            # Общий бросок с aiogram-путём (handlers.commands.coin.flip):
            # раньше эти 4 строки были продублированы и разъезжались.
            from handlers.commands.coin import flip
            await event.edit(await render_for_user(user_id, flip()[1]), parse_mode="html")
        elif head in AI_CMDS:
            from handlers.commands.ai import handle as handle_ai
            await handle_ai(user_id, event)
        elif head in OPENCODE_CMDS:
            from handlers.commands.opencode import handle as handle_opencode
            await handle_opencode(user_id, event)
        else:
            from utils.suggest import suggest_text
            hint = suggest_text(head)
            if hint:
                await event.edit(_command_card("Command", hint), parse_mode="html")

    # ------------------------------------------------------------------
    # Пользовательские модули
    # ------------------------------------------------------------------

    async def _handle_modules(self, user_id: str, event, head: str, tid: int = 0) -> bool:
        """Разобраться с dot-командой через модули.

        True — обработано (выполнен модулем или намеренно проигнорировано),
        идти дальше по системной цепочке не нужно.

        Правила:
          * выключенная юзером команда не выполняется вообще: ни системная,
            ни модульная (модуль может перехватить head только явно, через
            решение по конфликту — тогда он и считается «своим»);
          * если head зарегистрирован модулем этого юзера — выполняем
            модуль, системную команду не трогаем;
          * иначе False, идёт системная цепочка.
        """
        from utils import modules as _modules
        from utils.module_state import ModuleState

        if not _modules.modules_enabled():
            return False
        if not _modules.module_allowed(user_id):
            return False

        spec = _modules.find_command(user_id, head)
        if spec is None:
            # Модуль этот head не заявил. Если юзер выключил системную
            # команду — молчим: иначе он получил бы «пинг», который сам
            # отключил, а следом подсказку «возможно, это .ping?».
            if _modules.is_system_disabled(user_id, head):
                logger.info("system command %s disabled by uid=%s", head, user_id)
                return True
            return False

        if "telethon" not in getattr(spec.handler, "_module_paths", ("telethon", "bot")):
            return False

        # Модуль заявил СИСТЕМНУЮ команду, но юзер не подтвердил замену —
        # отдаём обработку системной цепочке. Иначе модуль, объявивший
        # `.ping`, молча перехватывал бы его сразу после установки.
        # Совпадает с правилом в handlers/commands/modules.py.
        if head in _modules.system_heads() and not _modules.is_system_disabled(user_id, head):
            return False

        # Гейт лимитов — тот же, что у встроенных команд. Бюджет
        # модульной команды прописан в `rate_limit_gate.BUDGETS` при
        # загрузке модуля (параметр `rate=` в `@command`).
        if gate.is_limited(head) and not gate.check(head, user_id):
            await event.edit(gate.RATE_LIMIT_TEXT, parse_mode="html")
            return True

        module = _modules.module_of_spec(user_id, spec)
        parts = (event.raw_text or "").strip().split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        ctx = _modules.Context(
            user_id=str(user_id),
            args=args,
            argv=args.split(),
            head=spec.head,
            event=event,
            client=getattr(event, "client", None),
            module=module,
            meta={
                "name": module.name if module else "",
                "title": module.title if module else "",
                "version": module.version if module else "",
                "author": module.author if module else "",
                "description": module.description if module else "",
            },
            state=ModuleState(user_id, module.name if module else spec.head),
            thread_id=tid or 0,
        )
        result = await _modules.run_command(user_id, spec, ctx)
        if isinstance(result, str) and result.strip():
            await event.edit(result, parse_mode="html")
        return True

    async def _apply_auto_tr_outgoing(self, user_id: str, event, text: str) -> None:
        """Переводит собственное сообщение в выбранный для чата язык."""
        from handlers.commands.tools import translate_with_ai
        from html import escape

        language = get_auto_tr_language(user_id, event.chat_id) or "ru"
        started = time.monotonic()
        lock_key = (user_id, event.chat_id)
        lock = self._tr_locks.get(lock_key)
        if lock is None:
            lock = self._tr_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            await asyncio.sleep(AUTO_TR_DELAY)
            try:
                await event.edit(
                    "<blockquote>[...] Перевожу через AI...</blockquote>",
                    parse_mode="html",
                )
                translated, error = await asyncio.wait_for(
                    translate_with_ai(user_id, language, text), timeout=60
                )
                elapsed = time.monotonic() - started
                if error or not translated:
                    await event.edit(text)
                    return
                if translated.strip() == text.strip():
                    await event.edit(
                        f"<blockquote>{escape(translated)}\n\n"
                        f"<i>Язык: {escape(language)} · {elapsed:.1f} с</i></blockquote>",
                        parse_mode="html",
                    )
                    return
                await event.edit(
                    f"<blockquote>{escape(translated)}\n\n"
                    f"<i>Перевод на {escape(language)} · {elapsed:.1f} с</i></blockquote>",
                    parse_mode="html",
                )
            except Exception as e:
                logger.warning(
                    f"_apply_auto_tr_outgoing failed for {user_id}/{event.chat_id}: {e}"
                )
                try:
                    await event.edit(text)
                except Exception:
                    pass
            finally:
                key = (str(user_id), event.chat_id, event.id)
                self._auto_tr_messages.discard(key)
                self._auto_tr_seen.pop(key, None)

    async def _handle_hash(self, event):
        from html import escape as _h
        from utils.hashing import hash_text, hash_bytes, ALGOS
        parts = event.raw_text.strip().split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        reply = await event.get_reply_message()
        reply_text = reply.raw_text if reply else None
        # Если reply содержит файл (document/photo/...) — берём байты из Telethon.
        data = None
        file_hint = None
        skip_for_format = False
        # mime и doc обязательно инициализировать ДО всех нижестоящих проверок —
        # если у реплая нет media (None / reply.media falsy), определение внутри
        # if ниже пропускается и getattr(doc, ...) падает с UnboundLocalError.
        mime = ""
        doc = None
        if reply and reply.media:
            doc = getattr(reply, "document", None)
            if doc:
                mime = getattr(doc, "mime_type", "") or ""
        # Sticker detection: scan document attributes. DocumentAttributeSticker
        # в Telethon API НЕ имеет поля `.animated` — тип анимации определяется
        # mime (TGS) и/или наличием DocumentAttributeVideo (WebM).
        attrs_for_sticker = getattr(doc, "attributes", None) or []
        has_sticker_attr = any(
            type(a).__name__ == "DocumentAttributeSticker" for a in attrs_for_sticker
        )
        animated_sticker_mimes = {
            "video/webm",
            "application/x-tgsticker",
            "application/x-tgs-sticker",
        }
        if has_sticker_attr and mime in animated_sticker_mimes:
            # TGS/WebM стикер: raw hash бессмысленен, hash-у не поддаётся.
            skip_for_format = True
        # Скачивание файла: ЭТОТ блок НЕ должен быть вложен в проверку стикеров —
        # иначе обычные файлы/фото/документы вообще не доходят до download_media.
        if not skip_for_format and (
            reply.photo
            or mime.startswith("image/")
            or mime.startswith("video/")
            or mime.startswith("audio/")
            or mime.startswith("text/")
            or mime.startswith("application/")
            or mime == "application/octet-stream"
        ):
            if not file_hint:
                if reply.photo:
                    file_hint = "фото"
                elif reply.document:
                    file_hint = getattr(doc, "file_name", None) or "документ"
                elif reply.video:
                    file_hint = "видео"
                elif reply.voice:
                    file_hint = "голосовое"
                elif reply.audio:
                    file_hint = getattr(reply.audio, "title", None) or "аудио"
            if has_sticker_attr and mime == "image/webp":
                # Статичный webp-стикер: перебиваем file_hint если был.
                file_hint = "стикер"
            if file_hint is not None:
                try:
                    data = await reply.download_media(
                        file=bytes,
                        progress_callback=_abort_big_download,
                    )
                except _TooBigDownload:
                    await event.edit(
                        _command_card("Hash", "[x] Файл слишком большой для хеширования (>25 МБ)."),
                        parse_mode="html",
                    )
                    return
                except Exception:
                    data = None
        if not args and not reply_text and data is None:
            await event.edit(
                _command_card("Hash", "[?] <code>.hash sha256 текст</code> или "
                "<code>.hash sha256</code> (reply на файл/фото/документ).\n"
                f"Алг: {', '.join(ALGOS)}."),
                parse_mode="html",
            )
            return
        ap = args.split(maxsplit=1) if args else []
        if ap and ap[0].lower() in ALGOS:
            algo = ap[0].lower()
            text = ap[1] if len(ap) > 1 else (reply_text or "")
        else:
            algo = "sha256"
            text = args or (reply_text or "")
        if data is not None:
            result = hash_bytes(algo, data)
            if isinstance(result, str) and result.startswith("[x]"):
                await event.edit(_command_card("Hash", result), parse_mode="html")
                return
            await event.edit(
                _command_card("Hash", (
                f"<b>🔐 {algo}</b>\n"
                f"<i>{_h(file_hint or 'файл')} · {len(data)} байт</i>\n"
                f"<code>{result}</code>")),
                parse_mode="html",
            )
            return
        if not text:
            if file_hint:
                await event.edit(
                    _command_card("Hash", f"[x] Не удалось скачать <code>{_h(file_hint)}</code>."),
                    parse_mode="html",
                )
                return
            await event.edit(_command_card("Hash", "[x] Нет текста."), parse_mode="html")
            return
        result = hash_text(algo, text)
        if isinstance(result, str) and result.startswith("[x]"):
            await event.edit(_command_card("Hash", result), parse_mode="html")
            return
        preview = _h(text[:50] + ("…" if len(text) > 50 else ""))
        await event.edit(
            _command_card("Hash", f"<b>{algo}</b>\n<i>{preview}</i>\n<code>{result}</code>"),
            parse_mode="html",
        )

    async def _handle_uuid(self, event):
        from utils.hashing import gen_uuids
        parts = event.raw_text.strip().split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        n = 1
        head = ""
        if args:
            head = args.split(maxsplit=1)[0]
            if head.lstrip("+-").isdigit():
                try:
                    n = max(1, min(int(head), 20))
                except ValueError:
                    n = 1
        body = "\n".join(f"<code>{u}</code>" for u in gen_uuids(n))
        await event.edit(
            _command_card("UUID", f"UUID4 x{n}\n{body}"),
            parse_mode="html",
        )

    async def _handle_b64(self, event):
        """`.b64` — единая реализация (utils/shared_cmd) для обоих путей."""
        from utils.hashing import b64_op
        from utils.shared_cmd import b64_body, parse_b64_args

        parts = event.raw_text.strip().split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        reply = await event.get_reply_message()
        reply_text = reply.raw_text if reply else None

        mode, text, url_safe = parse_b64_args(args)
        if not text:
            text = reply_text or ""
        if not text:
            await event.edit(
                _command_card(
                    "Base64",
                    "[?] <code>.b64 текст</code> — encode\n"
                    "<code>.b64 decode текст</code> — decode\n"
                    "<code>.b64 url …</code> — <b>url-safe</b> вариант (-_ вместо +/)\n"
                    "<code>.b64</code> (reply) — encode текста",
                ),
                parse_mode="html",
            )
            return

        result = b64_op(text, mode, url_safe=url_safe)
        if isinstance(result, str) and result.startswith("[x]"):
            await event.edit(_command_card("Base64", result), parse_mode="html")
            return
        await event.edit(
            _command_card("Base64", b64_body(mode, text, result, url_safe)),
            parse_mode="html",
        )

    async def _handle_timezone(self, user_id: str, event):
        """`.timezone` — единая реализация (utils/shared_cmd) для обоих путей.

        Раньше здесь была вторая копия с захардкоженными строками, которая
        разошлась с `handlers/commands/timezone.py` при первом же правке.
        """
        from utils.shared_cmd import timezone_body, timezone_parse
        from utils.storage import get_user_tz, set_user_tz

        parts = event.raw_text.strip().split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        uid = str(user_id)

        if is_reset_value(args):
            set_user_tz(uid, None)
            await event.edit(
                _command_card("Timezone", "[OK] TZ: <code>UTC</code> (по умолчанию)"),
                parse_mode="html",
            )
            return

        if not args:
            await event.edit(
                _command_card("Timezone", timezone_body(get_user_tz(uid))),
                parse_mode="html",
            )
            return

        ok, canonical = timezone_parse(args)
        if not ok or canonical is None:
            await event.edit(
                _command_card(
                    "Timezone",
                    f"[x] Не знаю таймзону: <code>{_esc(args.strip())}</code>\n"
                    "Примеры: <code>.timezone +3</code>, "
                    "<code>.timezone Europe/Moscow</code>, <code>.timezone МСК</code>.",
                ),
                parse_mode="html",
            )
            return
        set_user_tz(uid, canonical)
        await event.edit(
            _command_card("Timezone", f"[OK] TZ: <code>{_esc(canonical)}</code>"),
            parse_mode="html",
        )

    async def _handle_tr(self, user_id: str, event):
        from handlers.commands.tools import (
            _ISO_TO_NAME,
            _lang_display,
            _parse_tr_args,
            _do_tr_ai,
            _normalize_lang,
            translate_with_ai,
        )
        parts = event.raw_text.strip().split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        a = args.split(maxsplit=1) if args else []

        # Подкоманды: .tr auto [lang] / .tr stop / .tr list (Telethon-only).
        # Определяем здесь, до парсинга lang — чтобы не путать «auto» как язык.
        if a and a[0].lower() in ("auto", "stop", "list"):
            sub = a[0].lower()
            if sub == "list":
                await self._handle_tr_list(user_id, event)
                return
            # auto / stop — требуют текущий chat_id
            chat_id = event.chat_id
            if event.is_private:
                private_chat = await event.get_chat()
                if not getattr(private_chat, "bot", False):
                    pass
                else:
                    chat_title = "этот чат"
                    await event.edit(
                        _command_card("Translate", f"[i] {chat_title} — личка с ботом, auto-перевод недоступен."),
                        parse_mode="html",
                    )
                    return
            state = (sub == "auto")
            language = _normalize_lang(a[1] if len(a) > 1 else "ru")
            if state and language not in _ISO_TO_NAME:
                await event.edit(
                    _command_card(
                        "Translate",
                        "[?] Укажи язык: <code>.tr auto en</code> или "
                        "<code>.tr auto русский</code>.",
                    ), parse_mode="html"
                )
                return
            toggle_auto_tr_chat(str(user_id), chat_id, state, language)
            chat_title = "этот чат"
            try:
                chat = await event.get_chat()
                chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or "этот чат"
            except Exception:
                pass
            if state:
                msg = _command_card("Translate", (
                    f"[OK] Auto-перевод ВКЛЮЧЁН для <b>{_esc(chat_title)}</b> "
                    f"(<code>{chat_id}</code>).\n"
                    f"Язык: <code>{_esc(language)}</code>. Задержка 0.5с между ответами. "
                    f"<code>.tr stop</code> — выключить."
                ))
            else:
                msg = _command_card("Translate", f"[OK] Auto-перевод ВЫКЛЮЧЕН для <b>{_esc(chat_title)}</b> (<code>{chat_id}</code>).")
            await event.edit(msg, parse_mode="html")
            return

        # Получаем reply_text до парсинга — нужен для AI-перевода.
        reply_text = None
        reply = await event.get_reply_message()
        if reply:
            reply_text = reply.raw_text

        is_ai, lang, text_after = _parse_tr_args(args)
        target = text_after or reply_text or ""

        if is_ai:
            await event.edit(_command_card("Translate", "[…] AI-перевод…"), parse_mode="html")
            result = await _do_tr_ai(str(user_id), lang, target)
            await event.edit(result, parse_mode="html")
            return

        if not target:
            await event.edit(
                _command_card("Translate", "[?] <code>.tr en привет</code> или ответом на сообщение.\n"
                "<code>.tr en текст</code> — AI-перевод (сленг, стиль).\n"
                "Подкоманды: <code>.tr auto</code> / <code>.tr stop</code> / <code>.tr list</code>."),
                parse_mode="html",
            )
            return
        translated, error = await translate_with_ai(str(user_id), lang, target)
        if error:
            await event.edit(_command_card("Translate", f"[x] AI-перевод: {_esc(error)}"), parse_mode="html")
            return
        if not translated:
            await event.edit(_command_card("Translate", "[x] AI вернул пустой ответ."), parse_mode="html")
            return
        head = f"<b>→ {_esc(_lang_display(lang))}</b> <i>(AI)</i>"
        await event.edit(_command_card("Translate", f"{head}\n{_esc(translated)}"), parse_mode="html")

    async def _handle_tr_list(self, user_id: str, event):
        chat_ids = get_auto_tr_chats(str(user_id))
        if not chat_ids:
            await event.edit(
                _command_card("Translate", "[i] Список пуст. Включите: <code>.tr auto</code> в нужном чате."),
                parse_mode="html",
            )
            return
        client = self.get_client(str(user_id))
        lines = ["<b>Slim bot | Auto translate</b>"]
        for cid in chat_ids:
            title = str(cid)
            language = get_auto_tr_language(str(user_id), cid) or "ru"
            if client:
                try:
                    ent = await asyncio.wait_for(client.get_entity(cid), timeout=5)
                    title = getattr(ent, "title", None) or getattr(ent, "first_name", None) or str(cid)
                except Exception:
                    title = str(cid)
            # _esc защищает от HTML-инъекции через имя чата (как в _format_*_telethon)
            lines.append(f"• <b>{_esc(title)}</b> (<code>{cid}</code>) → <code>{_esc(language)}</code>")
        await event.edit(
            _command_card("Auto translate", "\n".join(lines)),
            parse_mode="html",
        )

    async def _handle_calc(self, event):
        from utils.calc import calc as do_calc
        from utils.escape import esc
        parts = event.raw_text.strip().split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        if not args:
            await event.edit(_command_card("Calc", "[?] <code>.calc 2 + 2 * 3</code>"), parse_mode="html")
            return
        try:
            res = do_calc(args)
        except Exception as e:
            await event.edit(
                _command_card("Calc", f"[x] {esc(type(e).__name__)}: {esc(str(e))}"),
                parse_mode="html",
            )
            return
        await event.edit(
            _command_card("Calc", f"<code>{esc(args.strip())}</code>\n= <b>{esc(res)}</b>"),
            parse_mode="html",
        )

    async def _handle_save(self, event):
        reply = await event.get_reply_message()
        if not reply:
            await event.edit(
                _command_card("Save", "[?] Ответь этой командой на сообщение."),
                parse_mode="html",
            )
            return
        try:
            await event.client.forward_messages("me", reply.id, event.chat_id)
            await event.edit(_command_card("Save", "[OK] Сохранено в Избранное."), parse_mode="html")
        except Exception as e:
            await event.edit(
                _command_card("Save", f"[x] {type(e).__name__}: {_esc(e)}"),
                parse_mode="html",
            )

    async def _handle_anim(
        self,
        user_id: str,
        event: events.NewMessage.Event,
        *,
        kind: str = "love",
    ):
        """``.love`` / ``.govno`` в Telethon-слое: анимация + удаляет команду.

        Telethon работает в СЕССИИ самого юзера, поэтому ``event.delete()``
        удаляет исходящее сообщение с командой надёжно (нет ограничений
        Bot API — там в private бот не может удалять чужие сообщения).

        Поддержка ``kind``:
        - ``"love"``: .love / .любовь (романтичная анимация).
        - ``"govno"``: .govno / .говно (какашка-анимация).
        Любое другое значение — silent return + debug log.
        """
        import asyncio
        from handlers.commands.love import (
            LOVE_ROWS, LOVE_MARKER, LOVE_PULSE,
            GOVNO_ROWS, GOVNO_MARKER, GOVNO_PULSE,
            make_frame,
        )
        from utils.texts import Texts

        if kind == "love":
            rows, marker, pulse = LOVE_ROWS, LOVE_MARKER, LOVE_PULSE
            final_text_obj = Texts.Love.LOVE_YOU
        elif kind == "govno":
            rows, marker, pulse = GOVNO_ROWS, GOVNO_MARKER, GOVNO_PULSE
            final_text_obj = Texts.Love.GOVNO_DONE
        else:
            logger.debug(f"_handle_anim: unknown kind={kind!r}")
            return

        msg = await event.respond(
            make_frame(rows, marker, marker),
            reply_to=telethon_reply_to(event),
        )
        anim_sent = msg is not None
        try:
            for _ in range(3):
                for variant in pulse:
                    try:
                        await msg.edit(make_frame(rows, marker, variant))
                    except Exception:
                        # MessageNotModified / flood — продолжаем кадры.
                        pass
                    await asyncio.sleep(0.45)
            try:
                await msg.edit(
                    await render_for_user(user_id, final_text_obj),
                    parse_mode="html",
                )
            except Exception:
                pass
        finally:
            # Удаляем исходную команду только если анимация реально
            # отправилась — иначе юзер теряет сообщение без всякого выхлопа.
            # (`return` в finally подавлял исключения выше — убрано.)
            if anim_sent:
                try:
                    await event.delete()
                except Exception as e:
                    logger.debug(f"_handle_anim {kind}: delete origin failed: {e}")

    async def _apply_nya(self, user_id: str, event, original_text: str) -> None:
        """Catgirl-rewrite обычного (не-dot) исходящего сообщения юзера.

        Запускается из ``_handle_outgoing`` через ``asyncio.create_task`` —
        не блокирует основной flow. После ``asyncio.sleep`` редактируем
        сообщение в стиле «кошко-девочка».

        Serialize по per-(user_id, chat_id) lock'у, чтобы burst из N
        сообщений не превращался в N одновременных event.edit'ов
        (что легко выедает FloodWait budget у Telethon-сессии).
        Аналогично self._tr_locks для auto-tr.

        Telethon event.edit(text) применяет text и к обычным сообщениям,
        и к caption'у медиа-сообщений (медиа остаётся). Поэтому работает и
        для фото со стикерами, и для текста.
        """
        from utils.catgirl import to_catgirl

        chat_id = event.chat_id
        lock_key = (str(user_id), chat_id)
        lock = self._nya_locks.get(lock_key)
        if lock is None:
            # get→setdefault без await между ними — для asyncio атомарно
            # (корутины не вытесняются без await), гонки двух Lock'ов нет.
            lock = self._nya_locks.setdefault(lock_key, asyncio.Lock())

        try:
            async with lock:
                # Небольшая задержка: основная Hook-цепочка уже ответит на
                # любые параллельные события; юзер увидит «сначала обычное
                # сообщение → затем редактирование в кошко-стиле» — это
                # естественно (имитирует авто-правку).
                await asyncio.sleep(NYA_EDIT_DELAY)
                new_text = to_catgirl(original_text)
                if new_text == original_text:
                    # Procedural rewriter вернул исходник (слишком короткий /
                    # без русских слов / слишком экзотичный) — edit'а не будет,
                    # и flood-control в Telegram не нагружаем.
                    return
                # Info log v3 — visible в production (bot.py выставляет
                # level=INFO для "utils.telethon_manager", debug НЕ показался
                # бы без ручной перенастройки). Помогает дебажить "иногда .ня
                # не редактирует".
                logger.info(
                    f"_apply_nya: chat={event.chat_id} msg={event.id} "
                    f"text_len={len(original_text)} -> {len(new_text)} "
                    f"changed={new_text.strip() != original_text.strip()}"
                )
                try:
                    await event.edit(new_text)
                except FloodWaitError as e:
                    logger.debug(f"_apply_nya: FloodWait {e.seconds}s, drop edit")
                except Exception as e:
                    # MessageNotModifiedError/etc — тихо глотаем.
                    logger.debug(f"_apply_nya: edit failed: {e}")
        except Exception as e:
            logger.debug(f"_apply_nya: outer error: {e}")

    async def _handle_incoming(self, user_id: str, event: events.NewMessage.Event):
        # Свои исходящие обрабатывает _handle_outgoing (там же index_live) —
        # здесь выходим сразу, иначе двойная обработка и двойное индексирование.
        if event.out:
            return
        # Не ждём запись в SQLite в update-loop: сетевые entity-resolve остаются
        # внутри фоновой задачи и не задерживают view-once/auto-translate hooks.
        if get_knowledge_selected_chats(user_id):
            asyncio.create_task(self._knowledge_collector.index_live(user_id, event))
        if not event.photo and not event.video:
            return
        media = event.media
        # Spoiler media is ordinary media with a hidden preview. It is not
        # view-once; only a positive Telegram TTL identifies self-destructing
        # media.
        ttl = getattr(media, "ttl_seconds", None) if media else None
        if ttl is None or ttl <= 0:
            return
        tid = thread_id_of(event)
        if not is_chat_watched(user_id, event.chat_id, tid):
            return
        if not is_photo_allowed(user_id, event.chat_id, tid):
            return
        if was_processed(event.chat_id, event.id, tid, user_id):
            return
        path = await self.download_view_once(user_id, event.chat_id, event.id, thread_id=tid)
        if not (path and self._bot):
            return
        try:
            chat = await event.get_chat()
        except Exception:
            chat = None
        chat_title = _esc(
            getattr(chat, "title", None)
            or getattr(chat, "first_name", None)
            or "чат"
        )
        topic_line = ""
        if tid:
            topic_name = await _topic_name_async(self.get_client(user_id), event.chat_id, tid)
            topic_line = f"Топик: <b>{_esc(topic_name)}</b>\n" if topic_name else f"Топик: <code>{tid}</code>\n"
        is_video = path.endswith((".mp4", ".mov", ".webm"))
        label = "видео" if is_video else "фото"
        caption = _view_once_caption(label, chat_title, topic_line, event.chat_id, tid)
        try:
            file = FSInputFile(path)
            if is_video:
                await self._bot.send_video(chat_id=int(user_id), video=file, caption=caption)
            else:
                await self._bot.send_photo(chat_id=int(user_id), photo=file, caption=caption)
        except Exception as e:
            try:
                await self._bot.send_message(
                    chat_id=int(user_id),
                    text=f"{caption}\n\n❌ Не удалось отправить файл: {_esc(e)}",
                )
            except Exception:
                pass
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def _view_once_caption(label: str, chat_title: str, topic_line: str, chat_id: int, thread_id: int) -> str:
    """Единый формат уведомления о сохранённом одноразовом медиа.

    Pure builder (без I/O) — покрыт тестами. Отправляется caption'ом
    к фото/видео от бота в личку.
    """
    body = (
        f"📸 Одноразовое {label} сохранено\n"
        f"Чат: <b>{chat_title}</b>\n"
        f"{topic_line}"
        f"<code>chat_id: {chat_id}</code>\n"
        f"<code>thread_id: {thread_id}</code>"
    )
    return _command_card("View-once", body)


telethon_manager = TelethonManager()


# ---------- Telethon entity formatters (используют MTProto User c phone/lang/etc) ----------


_TARGET_SPLIT_RE = re.compile(r"[\s,]+")


def parse_dot_targets(args: str, limit: int = DOT_TARGET_LIMIT) -> list[str]:
    """Парсит несколько @username / ID из dot-аргументов (.who @user1 @user2).

    Дедуплицирует case-insensitive, лимит по умолчанию 5.
    """
    if not args:
        return []
    tokens = _TARGET_SPLIT_RE.split(args.strip())
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


def _detect_avatar_ext(data: bytes) -> str:
    """Определяет расширение файла аватарки по magic bytes.

    Нужно чтобы передать имя файла в Telethon send_file — без этого Telegram
    получает "unnamed" без MIME и не строит превью-картинку нормально.

    Поддержка: JPEG, PNG, GIF, WebP (анимированные аватарки Premium — WEBP).
    Fallback — .jpg (большинство стандартных аватарок Telegram именно JPEG).
    """
    if not data:
        return "avatar.jpg"
    if data.startswith(b"\xff\xd8\xff"):
        return "avatar.jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "avatar.png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "avatar.gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "avatar.webp"
    return "avatar.jpg"


def _yn(v) -> str:
    return "✅" if v else "—"


def _telethon_status(status) -> str:
    if status is None:
        return "—"
    name = type(status).__name__
    mapping = {
        "UserStatusOnline": "🟢 онлайн",
        "UserStatusOffline": "⚪ оффлайн",
        "UserStatusRecently": "недавно",
        "UserStatusLastWeek": "на этой неделе",
        "UserStatusLastMonth": "в этом месяце",
        "UserStatusEmpty": "скрыт",
    }
    base = mapping.get(name, name)
    was = getattr(status, "was_online", None)
    if was:
        base += f" ({was.strftime('%Y-%m-%d %H:%M')} UTC)"
    return base


def _format_me_telethon(s, full_user=None) -> str:
    """``.me`` карточка.

    Раньше был параметр ``premium_render``, который НИ РАЗУ не читался в теле
    функции, хотя docstring утверждал, что он «управляет рендером эмодзи»,
    а все три вызова передавали ``is_entity_premium(...)`` (P3.10).
    Параметр удалён. Индикатор ``Premium: ✅`` внутри карточки
    отражает собственный Premium цели (для .me это сам юзер)."""
    if not s:
        return _command_card("Me", "[x] Не удалось получить данные.")
    first = getattr(s, "first_name", None) or "—"
    last = getattr(s, "last_name", None) or ""
    full = (first + " " + last).strip()
    phone = getattr(s, "phone", None)
    lang = getattr(s, "lang_code", None)
    has_premium = getattr(s, "premium", False)

    parts = [f"Имя: {_esc(full)}"]
    unames = _usernames_of(s, full_user)
    if unames:
        parts.append(_render_usernames(unames))
    if phone:
        parts.append(f"Телефон: {_esc(phone)}")
    if lang:
        parts.append(f"Язык: {_esc(lang)}")
    if has_premium:
        parts.append("Premium: ✅")
    if full_user is not None:
        about = getattr(full_user, "about", None) or ""
        if about:
            short = about.strip().replace("\n", " ")
            if len(short) > 200:
                short = short[:200] + "…"
            parts.append("")
            parts.append(f"<i>{_esc(short)}</i>")
    return _command_card(
        "Me",
        f"ID: <code>{s.id}</code>\n" + chr(10).join(parts),
    )


def _format_chat_telethon(chat, chat_id: int, full_chat=None) -> str:
    """``.chat`` карточка (``premium_render`` удалён — см. _format_me_telethon)."""
    title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or "—"
    ctype = type(chat).__name__
    members = getattr(chat, "participants_count", None)
    verified = getattr(chat, "verified", False)
    scam = getattr(chat, "scam", False)
    fake = getattr(chat, "fake", False)
    restricted = getattr(chat, "restricted", False)

    parts = [
        f"ID: <code>{chat_id}</code>",
        f"Тип: {_esc(ctype)}",
        f"Название: {_esc(title)}",
    ]
    unames = _usernames_of(chat, full_chat)
    if unames:
        parts.append(_render_usernames(unames))
    if members is not None:
        parts.append(f"Участников: {_esc(members)}")
    flags = []
    if verified: flags.append("✓ verified")
    if scam: flags.append("⚠ scam")
    if fake: flags.append("⚠ fake")
    if restricted: flags.append("⚠ restricted")
    if flags:
        parts.append("Флаги: " + ", ".join(flags))
    if full_chat is not None:
        about = getattr(full_chat, "about", None) or ""
        if about:
            short = about.strip().replace("\n", " ")
            if len(short) > 200:
                short = short[:200] + "…"
            parts.append("")
            parts.append(f"<i>{_esc(short)}</i>")
    return _command_card("Chat", chr(10).join(parts))


def _format_who_telethon(s, full_user=None) -> str:
    """``.who`` карточка (single + multi-target).

    Параметр ``premium_render`` удалён (P3.10): он никогда не читался в теле
    функции, в отличие от docstring, который утверждал обратное. Флаг
    ``premium`` внутри карточки по-прежнему отражает Premium САМОЙ цели.
    """
    if not s:
        return _command_card("Who", "[x] Не удалось получить данные.")
    first = getattr(s, "first_name", None) or ""
    last = getattr(s, "last_name", None) or ""
    full = (first + " " + last).strip() or "—"
    phone = getattr(s, "phone", None)
    lang = getattr(s, "lang_code", None)
    is_bot = getattr(s, "bot", False)
    premium = getattr(s, "premium", False)
    verified = getattr(s, "verified", False)
    scam = getattr(s, "scam", False)
    fake = getattr(s, "fake", False)
    restricted = getattr(s, "restricted", False)
    support = getattr(s, "support", False)
    deleted = getattr(s, "deleted", False)
    mutual = getattr(s, "mutual_contact", False)
    contact = getattr(s, "contact", False)
    status = _telethon_status(getattr(s, "status", None))

    parts = [f"ID: <code>{s.id}</code>", f"Имя: {_esc(full)}"]
    unames = _usernames_of(s, full_user)
    if unames:
        parts.append(_render_usernames(unames))
    if phone:
        parts.append(f"Телефон: {_esc(phone)}")
    if lang:
        parts.append(f"Язык: {_esc(lang)}")
    parts.append(f"Статус: {status}")

    flags = []
    if is_bot: flags.append("bot")
    if premium: flags.append("premium")
    if verified: flags.append("✓ verified")
    if scam: flags.append("scam")
    if fake: flags.append("fake")
    if restricted: flags.append("restricted")
    if support: flags.append("support")
    if deleted: flags.append("deleted")
    if contact or mutual:
        flags.append("contact" + (" (mutual)" if mutual else ""))
    if flags:
        parts.append("Флаги: " + ", ".join(flags))

    if full_user is not None:
        about = getattr(full_user, "about", None) or ""
        if about:
            short = about.strip().replace("\n", " ")
            if len(short) > 200:
                short = short[:200] + "…"
            parts.append("")
            parts.append(f"<i>{_esc(short)}</i>")

    return _command_card("Who", chr(10).join(parts))
