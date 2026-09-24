"""Resumable Telegram history collector for `.ии база`."""

import asyncio
import logging
import time
from datetime import datetime, timezone

from telethon.errors import FloodWaitError

from config import (
    KNOWLEDGE_CHECKPOINT_EVERY,
    KNOWLEDGE_HISTORY_WAIT_TIME,
    KNOWLEDGE_PROGRESS_INTERVAL,
    KNOWLEDGE_MAX_MESSAGES_PER_OWNER,
)
from utils import knowledge_db
from utils.storage import get_knowledge_selected_chats


logger = logging.getLogger(__name__)


def _thread_id(message) -> int:
    reply_to = getattr(message, "reply_to", None)
    if not reply_to:
        return 0
    top = getattr(reply_to, "reply_to_top_id", None)
    if top:
        return int(top)
    if getattr(reply_to, "forum_topic", False):
        root = getattr(reply_to, "reply_to_msg_id", None)
        return int(root) if root else 0
    return 0


def _entity_label(entity, fallback: int) -> tuple[str, str]:
    title = (
        getattr(entity, "title", None)
        or getattr(entity, "first_name", None)
        or getattr(entity, "username", None)
        or str(fallback)
    )
    return str(title), str(getattr(entity, "username", None) or "")


class KnowledgeCollector:
    """One sequential collection task per Telethon account."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._last_progress: dict[str, float] = {}
        self._start_locks: dict[str, asyncio.Lock] = {}
        self._live_prune_counter: dict[str, int] = {}

    def is_running(self, owner_id: str) -> bool:
        task = self._tasks.get(str(owner_id))
        return bool(task and not task.done())

    async def start(self, owner_id: str, client) -> tuple[bool, str]:
        owner_id = str(owner_id)
        # Serialize старты: без lock'а два одновременных start() (например,
        # рестарт бота + ручной запуск юзером) оба проходят is_running(),
        # и создаются ДВА collector-таска для одного владельца.
        lock = self._start_locks.setdefault(owner_id, asyncio.Lock())
        async with lock:
            if self.is_running(owner_id):
                return False, "[i] Сбор уже выполняется. <code>.ии база статус</code> покажет прогресс."
            if not get_knowledge_selected_chats(owner_id):
                return False, "[?] Сначала выбери чаты в личке с ботом: <code>.ии база</code>."

            me = await client.get_me()
            client._knowledge_self_id = int(me.id)
            state = knowledge_db.get_collection(owner_id)
            progress_id = state.get("progress_message_id") if state else None
            if not progress_id:
                progress = await client.send_message("me", "<b>База знаний</b>\nПодготовка сбора…", parse_mode="html")
                progress_id = progress.id
            knowledge_db.begin_collection(owner_id, progress_message_id=progress_id)
            self._tasks[owner_id] = asyncio.create_task(self._run(owner_id, client))
            return True, "[OK] Сбор запущен. Прогресс отправлен в Избранное."

    async def stop(self, owner_id: str) -> bool:
        owner_id = str(owner_id)
        task = self._tasks.get(owner_id)
        if not task or task.done():
            return False
        knowledge_db.update_collection(owner_id, status="paused_manual")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def resume_pending(self, owner_id: str, client) -> None:
        state = knowledge_db.get_collection(owner_id)
        # FloodWait требует явного ручного решения пользователя: не возобновляем
        # его молча при рестарте и не создаём новый лимит сразу после паузы.
        if state and state.get("status") == "running":
            await self.start(owner_id, client)

    async def index_live(self, owner_id: str, event) -> None:
        """Adds newly received text after a completed or active collection."""
        state = knowledge_db.get_collection(owner_id)
        if not state or state.get("status") not in {"running", "completed"}:
            return
        if int(event.chat_id) not in get_knowledge_selected_chats(owner_id):
            return
        message = getattr(event, "message", event)
        text = (getattr(message, "raw_text", None) or getattr(message, "message", None) or "").strip()
        if not text or event.chat_id == getattr(event.client, "_knowledge_self_id", None):
            return
        try:
            entity = await event.get_chat()
            sender = await event.get_sender()
            await self._store_message(owner_id, message, event.chat_id, entity, sender)
            # Live-индексация после завершённого сбора растёт неограниченно:
            # периодический prune держит владельца в рамках лимита.
            counter = self._live_prune_counter.get(owner_id, 0) + 1
            self._live_prune_counter[owner_id] = counter
            if counter % 500 == 0:
                knowledge_db.prune_owner(owner_id, KNOWLEDGE_MAX_MESSAGES_PER_OWNER)
        except Exception as exc:
            logger.debug("knowledge live index failed uid=%s: %s", owner_id, exc)

    async def _run(self, owner_id: str, client) -> None:
        try:
            me = await client.get_me()
            self_id = int(me.id)
            # Used by index_live to skip collector status in Saved Messages.
            client._knowledge_self_id = self_id
            dialogs = []
            selected_chats = get_knowledge_selected_chats(owner_id)
            async for dialog in client.iter_dialogs(folder=None, ignore_migrated=False):
                if int(dialog.id) != self_id and int(dialog.id) in selected_chats:
                    dialogs.append(dialog)
            knowledge_db.update_collection(owner_id, total_dialogs=len(dialogs), status="running")

            completed = 0
            for dialog in dialogs:
                if asyncio.current_task().cancelled():
                    raise asyncio.CancelledError
                await self._collect_dialog(owner_id, client, dialog)
                completed += 1
                state = knowledge_db.get_collection(owner_id) or {}
                knowledge_db.update_collection(owner_id, completed_dialogs=completed,
                                               indexed_messages=knowledge_db.count_messages(owner_id))
                await self._progress(owner_id, client, force=True)

            knowledge_db.update_collection(
                owner_id, status="completed", current_chat_id=None, current_chat_title="",
                completed_dialogs=len(dialogs), indexed_messages=knowledge_db.count_messages(owner_id),
                paused_until=None, error="",
            )
            await self._progress(owner_id, client, force=True)
        except FloodWaitError as exc:
            until = datetime.fromtimestamp(time.time() + exc.seconds, timezone.utc).isoformat()
            knowledge_db.update_collection(owner_id, status="paused_rate_limit", paused_until=until,
                                           error=f"FloodWait {exc.seconds}s")
            try:
                await self._progress(owner_id, client, force=True)
            except FloodWaitError:
                pass
            logger.warning("knowledge collector paused uid=%s FloodWait=%ss", owner_id, exc.seconds)
        except asyncio.CancelledError:
            # Задача уже отменена: DB-обновление синхронное, а сетевой прогресс —
            # best-effort с таймаутом (иначе отмена может зависнуть на RPC).
            state = knowledge_db.get_collection(owner_id) or {}
            if state.get("status") == "running":
                knowledge_db.update_collection(owner_id, status="paused_manual")
            try:
                await asyncio.wait_for(self._progress(owner_id, client, force=True), timeout=10)
            except Exception:
                pass
            raise
        except Exception as exc:
            logger.exception("knowledge collector failed uid=%s", owner_id)
            knowledge_db.update_collection(owner_id, status="failed", error=type(exc).__name__)
            await self._progress(owner_id, client, force=True)
        finally:
            self._tasks.pop(owner_id, None)

    async def _collect_dialog(self, owner_id: str, client, dialog) -> None:
        chat_id = int(dialog.id)
        title, username = _entity_label(dialog.entity, chat_id)
        old = knowledge_db.get_dialog(owner_id, chat_id) or {}
        snapshot_top_id = int(getattr(getattr(dialog, "message", None), "id", 0) or 0)
        latest_seen = int(old.get("latest_seen_id") or 0)
        # Completed dialogs only need a delta pass. Incomplete dialogs are safely
        # replayed from the last durable low cursor; UPSERT removes overlap duplicates.
        full_history = old.get("status") != "completed"
        knowledge_db.upsert_dialog(
            owner_id, chat_id, title=title, username=username, status="running",
            snapshot_top_id=snapshot_top_id, oldest_processed_id=int(old.get("oldest_processed_id") or 0),
            latest_seen_id=latest_seen,
        )
        knowledge_db.update_collection(owner_id, current_chat_id=chat_id, current_chat_title=title)
        await self._progress(owner_id, client)

        processed = 0
        oldest = int(old.get("oldest_processed_id") or 0)
        kwargs = {"wait_time": KNOWLEDGE_HISTORY_WAIT_TIME}
        if not full_history and latest_seen:
            kwargs["min_id"] = latest_seen
        elif oldest:
            # Replay a small safe overlap after a crash before continuing older history.
            kwargs["offset_id"] = oldest + 1
        if full_history and snapshot_top_id:
            # Lock the historical scan to the known top. New messages are picked
            # up by the delta pass below, so busy chats cannot extend this loop.
            kwargs["max_id"] = snapshot_top_id + 1

        async for message in client.iter_messages(dialog.entity, **kwargs):
            # Не резолвим автора отдельным RPC для каждого исторического сообщения:
            # это резко повышает шанс FloodWait на больших аккаунтах. Telethon часто
            # уже приложил sender в entities; иначе сохраняем стабильный sender_id.
            sender = getattr(message, "sender", None)
            await self._store_message(owner_id, message, chat_id, dialog.entity, sender)
            processed += 1
            mid = int(message.id)
            latest_seen = max(latest_seen, mid)
            if full_history:
                oldest = mid
            if processed % KNOWLEDGE_CHECKPOINT_EVERY == 0:
                knowledge_db.upsert_dialog(
                    owner_id, chat_id, title=title, username=username, status="running",
                    snapshot_top_id=snapshot_top_id, oldest_processed_id=oldest,
                    latest_seen_id=latest_seen,
                )
                # Retention: старые записи владельца за потолком вычищаются
                # на чекпоинтах, чтобы база не росла бесконечно.
                knowledge_db.prune_owner(owner_id, KNOWLEDGE_MAX_MESSAGES_PER_OWNER)
                knowledge_db.update_collection(
                    owner_id, indexed_messages=knowledge_db.count_messages(owner_id),
                )
                await self._progress(owner_id, client)

        if full_history and snapshot_top_id:
            # Close the race between the first snapshot and old-history scan.
            # `min_id` without a finite upper boundary can keep a Telethon
            # iterator alive in a busy chat. Take another top snapshot first.
            top_now = await client.get_messages(dialog.entity, limit=1)
            delta_top_id = int(top_now[0].id) if top_now else snapshot_top_id
            if delta_top_id > snapshot_top_id:
                async for message in client.iter_messages(
                    dialog.entity,
                    min_id=snapshot_top_id,
                    max_id=delta_top_id + 1,
                    wait_time=KNOWLEDGE_HISTORY_WAIT_TIME,
                ):
                    sender = getattr(message, "sender", None)
                    await self._store_message(owner_id, message, chat_id, dialog.entity, sender)
                    latest_seen = max(latest_seen, int(message.id))

        knowledge_db.upsert_dialog(
            owner_id, chat_id, title=title, username=username, status="completed",
            snapshot_top_id=snapshot_top_id, oldest_processed_id=oldest,
            latest_seen_id=max(latest_seen, snapshot_top_id),
        )
        knowledge_db.update_collection(
            owner_id, indexed_messages=knowledge_db.count_messages(owner_id),
        )

    async def _store_message(self, owner_id: str, message, chat_id: int, entity, sender) -> None:
        text = (getattr(message, "raw_text", None) or getattr(message, "message", None) or "").strip()
        if not text:
            return
        title, username = _entity_label(entity, chat_id)
        sender_name, _ = _entity_label(sender, getattr(message, "sender_id", 0) or 0)
        date = getattr(message, "date", None)
        sent_at = date.astimezone(timezone.utc).isoformat() if date else ""
        knowledge_db.upsert_message(
            owner_id, chat_id=chat_id, thread_id=_thread_id(message), message_id=int(message.id),
            text=text, chat_title=title, chat_username=username,
            sender_id=getattr(message, "sender_id", None), sender_name=sender_name, sent_at=sent_at,
        )

    async def _progress(self, owner_id: str, client, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress.get(owner_id, 0) < KNOWLEDGE_PROGRESS_INTERVAL:
            return
        self._last_progress[owner_id] = now
        state = knowledge_db.get_collection(owner_id)
        if not state or not state.get("progress_message_id"):
            return
        status = state.get("status", "running")
        labels = {
            "running": "Сбор выполняется", "completed": "Сбор завершён",
            "paused_rate_limit": "Сбор остановлен: лимит Telegram",
            "paused_manual": "Сбор остановлен вручную", "failed": "Сбор завершился с ошибкой",
        }
        lines = [f"<b>База знаний</b>\n{labels.get(status, status)}"]
        lines.append(f"Чаты: <code>{state.get('completed_dialogs', 0)}</code> / <code>{state.get('total_dialogs', 0)}</code>")
        lines.append(f"Текстовые сообщения: <code>{state.get('indexed_messages', 0)}</code>")
        if state.get("current_chat_title"):
            from utils.escape import esc
            lines.append(f"Текущий чат: {esc(state['current_chat_title'][:120])}")
        if state.get("paused_until"):
            lines.append(f"Продолжить после: <code>{state['paused_until'][:19]}</code>")
        if state.get("error"):
            lines.append(f"Причина: <code>{state['error']}</code>")
        try:
            message = await client.get_messages("me", ids=int(state["progress_message_id"]))
            if message:
                await message.edit("\n".join(lines), parse_mode="html")
            else:
                sent = await client.send_message("me", "\n".join(lines), parse_mode="html")
                knowledge_db.update_collection(owner_id, progress_message_id=sent.id)
        except FloodWaitError as exc:
            # Progress-сообщение не критично: не роняем сбор из-за лимита
            # редактирований. Тихо пропускаем, следующий чекпоинт попробует
            # снова (до этого raise'd → collection могла умереть здесь).
            logger.debug("knowledge progress FloodWait uid=%s sleep=%ss", owner_id, exc.seconds)
            await asyncio.sleep(min(int(exc.seconds), 30))
        except Exception as exc:
            logger.debug("knowledge progress update failed uid=%s: %s", owner_id, exc)


knowledge_collector = KnowledgeCollector()
