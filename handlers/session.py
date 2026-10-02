import asyncio
import logging
import secrets
import time
from collections import defaultdict, deque

from aiogram import Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    PhoneNumberOccupiedError,
    FloodWaitError,
)

from config import (
    API_ID,
    API_HASH,
    AUTH_ATTEMPT_WINDOW,
    AUTH_COOLDOWN,
    AUTH_MAX_ATTEMPTS,
    SESSION_ALLOWLIST,
)
from utils.premium import invalidate_premium_cache
from utils.storage import user_sessions, save_user_sessions, session_path
from utils.telethon_manager import auth_states, telethon_manager

logger = logging.getLogger(__name__)
router = Router()

#: uid -> время последней активности (для prune обоих словарей).
_AUTH_SEEN: dict[str, float] = {}
_AUTH_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_AUTH_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)

#: Порог, после которого подчищаем словари. Раньше порог был только у
#: `_AUTH_ATTEMPTS`, а `_AUTH_LOCKS` (создаётся на КАЖДОМ чтении
#: `_AUTH_LOCKS[uid]` в трёх местах) рос бесконечно: по одному Lock на
#: каждого юзера, который хотя бы раз коснулся аутентификации.
_AUTH_PRUNE_THRESHOLD = 2000
_AUTH_PRUNE_KEEP = 1000


def _prune_auth_state(now: float) -> int:
    """Вычистить истёкшие записи обоих auth-словарей.

    Два независимых признака устаревания:

    * ``_AUTH_SEEN[uid]`` старше окна — юзер давно не появлялся, поэтому
      ни Lock, ни очередь попыток ему не нужны;
    * сама очередь ``_AUTH_ATTEMPTS[uid]`` пуста или протухла (в т.ч.
      записи, попавшие в словарь мимо ``_touch_auth_state``).

    Плюс аварийный потолок: даже если что-то формально не протухло,
    словари не дают расти бесконечно.

    Возвращает число удалённых записей.
    """
    removed = 0

    for uid in list(_AUTH_SEEN):
        if now - _AUTH_SEEN[uid] <= AUTH_ATTEMPT_WINDOW:
            continue
        _AUTH_SEEN.pop(uid, None)
        if _AUTH_LOCKS.pop(uid, None) is not None:
            removed += 1
        if _AUTH_ATTEMPTS.pop(uid, None) is not None:
            removed += 1

    for uid, queue in list(_AUTH_ATTEMPTS.items()):
        while queue and now - queue[0] > AUTH_ATTEMPT_WINDOW:
            queue.popleft()
        if not queue:
            _AUTH_ATTEMPTS.pop(uid, None)
            _AUTH_SEEN.pop(uid, None)
            if _AUTH_LOCKS.pop(uid, None) is not None:
                removed += 1
            removed += 1

    while max(len(_AUTH_SEEN), len(_AUTH_ATTEMPTS), len(_AUTH_LOCKS)) > _AUTH_PRUNE_KEEP:
        uid = next(iter(_AUTH_SEEN))
        _AUTH_SEEN.pop(uid, None)
        if _AUTH_LOCKS.pop(uid, None) is not None:
            removed += 1
        if _AUTH_ATTEMPTS.pop(uid, None) is not None:
            removed += 1

    return removed


def _auth_lock(uid: str) -> asyncio.Lock:
    """Per-user блокировка аутентификации.

    Оборачивает defaultdict: помимо создания Lock'а отмечает активность,
    чтобы prune знал, какие записи ещё нужны. Раньше здесь стояло
    `_AUTH_LOCKS[uid]` напрямую — чтение defaultdict СОЗДАВАЛО запись, и она
    жила до конца процесса.
    """
    lock = _AUTH_LOCKS[uid]
    _touch_auth_state(uid, time.monotonic())
    return lock


def _touch_auth_state(uid: str, now: float) -> None:
    """Отметить активность юзера и устроить prune при необходимости."""
    _AUTH_SEEN[uid] = now
    # Порог считаем по ОБОИМ словарям: попытки могут попасть в
    # _AUTH_ATTEMPTS и мимо _AUTH_SEEN (например, в тестах или при
    # восстановлении после рестарта), и тогда один рос бы незаметно.
    if max(len(_AUTH_SEEN), len(_AUTH_ATTEMPTS), len(_AUTH_LOCKS)) > _AUTH_PRUNE_THRESHOLD:
        _prune_auth_state(now)


def _allow_auth_attempt(uid: str) -> bool:
    now = time.monotonic()
    attempts = _AUTH_ATTEMPTS.get(uid)
    if attempts:
        while attempts and now - attempts[0] > AUTH_ATTEMPT_WINDOW:
            attempts.popleft()
        if not attempts:
            _AUTH_ATTEMPTS.pop(uid, None)
            attempts = None
    if attempts:
        _touch_auth_state(uid, now)
        if now - attempts[-1] < AUTH_COOLDOWN:
            return False
        if len(attempts) >= AUTH_MAX_ATTEMPTS:
            return False
        attempts.append(now)
        return True
    _touch_auth_state(uid, now)
    _AUTH_ATTEMPTS[uid].append(now)
    return True


def _rand_delay():
    return secrets.randbelow(2000) / 1000 + 0.3


def session_allowed(uid: str) -> bool:
    """Разрешено ли этому uid подключать Telethon-сессию.

    Пустой SESSION_ALLOWLIST = открытый режим (обратная совместимость
    одиночного использования). Непустой = только перечисленные id; это
    обязательный гейт для публичного бота, иначе любой, кто его нашёл,
    подключает свой аккаунт и получает доступ к общему LLM-ключу.
    """
    if not SESSION_ALLOWLIST:
        return True
    return str(uid) in SESSION_ALLOWLIST


def allowlist_active() -> bool:
    return bool(SESSION_ALLOWLIST)


WHY_TEXT = (
    "<b>[?] Зачем это нужно?</b>\n\n"
    "<b>Обычный режим:</b>\n"
    "[.] Команды только в личных чатах\n"
    "[.] Нет доступа к одноразовым фото\n"
    "[.] Нельзя указать @username\n\n"
    "<b>Дополнительно:</b>\n"
    "[+] Команды работают в любых чатах\n"
    "[+] Одноразовые фото сохраняются\n"
    "[+] Работает <code>.watch @username</code>\n\n"
    "Нажми кнопку — всё настроится само."
)

WHY_ACTIVE = (
    "<b>[?] Дополнительные возможности</b>\n\n"
    "✅ Уже <b>включено</b>:\n"
    "[+] Команды работают в любых чатах\n"
    "[+] Одноразовые фото сохраняются\n"
    "[+] Работает <code>.watch @username</code>\n\n"
    "Выключить можно кнопкой ниже или командой /logout."
)


def _p_kbd(s: str = "") -> InlineKeyboardMarkup:
    btns = []
    for row in [("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9")]:
        btns.append([InlineKeyboardButton(text=d, callback_data=f"p:{d}") for d in row])
    row = []
    if s:
        row.append(InlineKeyboardButton(text="⌫", callback_data="pb"))
    else:
        row.append(InlineKeyboardButton(text=" ", callback_data="_x"))
    row.append(InlineKeyboardButton(text="0", callback_data="p:0"))
    if s and len(s) >= 7:
        row.append(InlineKeyboardButton(text="✅", callback_data="pgo"))
    else:
        row.append(InlineKeyboardButton(text=" ", callback_data="_x"))
    btns.append(row)
    return InlineKeyboardMarkup(inline_keyboard=btns)


def _d_kbd(s: str = "") -> InlineKeyboardMarkup:
    btns = []
    for row in [("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9")]:
        btns.append([InlineKeyboardButton(text=d, callback_data=f"d:{d}") for d in row])
    row = []
    if s:
        row.append(InlineKeyboardButton(text="⌫", callback_data="ddel"))
    row.append(InlineKeyboardButton(text="0", callback_data="d:0"))
    if s:
        row.append(InlineKeyboardButton(text="[OK]", callback_data="dgo"))
    else:
        row.append(InlineKeyboardButton(text="   ", callback_data="_x"))
    btns.append(row)
    return InlineKeyboardMarkup(inline_keyboard=btns)


@router.callback_query(lambda c: c.data == "w1")
async def why_cb(callback: types.CallbackQuery):
    from utils.storage import session_exists
    uid = str(callback.from_user.id) if callback.from_user else ""
    if uid and session_exists(uid):
        await callback.message.edit_text(
            WHY_ACTIVE,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="[x] Выключить", callback_data="logout_ask")],
                [InlineKeyboardButton(text="[<-] Назад", callback_data="b1")],
            ]),
        )
    else:
        await callback.message.edit_text(
            WHY_TEXT,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="[+] Включить", callback_data="c1")],
                [InlineKeyboardButton(text="[<-] Назад", callback_data="b1")],
            ]),
        )
    await callback.answer()


@router.callback_query(lambda c: c.data == "b1")
async def back_cb(callback: types.CallbackQuery):
    from handlers.commands._base import format_help
    from handlers.commands.help import help_keyboard
    from utils.storage import session_exists
    has_ss = callback.from_user and session_exists(str(callback.from_user.id))
    await callback.message.edit_text(format_help(has_ss), reply_markup=help_keyboard(has_ss))
    await callback.answer()


@router.callback_query(lambda c: c.data == "c1")
async def connect_cb(callback: types.CallbackQuery):
    uid = str(callback.from_user.id)
    if not session_allowed(uid):
        logger.warning("connect: uid=%s rejected (not in SESSION_ALLOWLIST)", uid)
        await callback.answer("Подключение недоступно", show_alert=True)
        return
    if not allowlist_active():
        # Открытый режим — это осознанный выбор оператора, но о нём стоит
        # знать: любой нашедший бота может подключить свой аккаунт.
        logger.warning(
            "connect: uid=%s accepted in OPEN mode (SESSION_ALLOWLIST is empty)", uid
        )
    logger.info(f"connect: uid={uid}")
    if uid in auth_states and auth_states[uid].get("step") in {"code", "2fa", "starting"}:
        await callback.answer("Подключение уже выполняется")
        return
    auth_states[uid] = {"step": "phone", "s": "", "_ts": time.time()}
    await callback.message.edit_text(
        "<b>Шаг 1: Номер телефона</b>\n\n"
        "Введи цифры номера кнопками ниже. Код страны обязателен — "
        "например, <code>79001234567</code>. Префикс <b>+</b> бот добавит сам.\n\n"
        "Когда закончишь — нажми <b>✅</b>.\n\n"
        "<b>Твой номер:</b> <code>+</code>",
        reply_markup=_p_kbd(""),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("p:") or c.data in ("pb", "pgo", "_x"))
async def phone_kb(callback: types.CallbackQuery):
    uid = str(callback.from_user.id)
    state = auth_states.get(uid)
    if not state or state["step"] != "phone":
        await callback.answer()
        return
    d = callback.data
    if d == "_x":
        await callback.answer()
        return
    if d.startswith("p:"):
        ch = d.split(":", 1)[1]
        if len(ch) != 1 or not ch.isdigit():
            await callback.answer()
            return
        state["s"] += ch
        disp = state["s"]
        await callback.answer(f"+{disp}")
        try:
            await callback.message.edit_text(
                f"<b>Шаг 1: Номер телефона</b>\n\n"
                f"Вводи цифры только кнопками ниже.\n"
                f"Когда закончишь — нажми <b>✅</b>.\n\n"
                f"<b>Твой номер:</b> <code>+{disp}</code>",
                reply_markup=_p_kbd(disp),
            )
        except TelegramBadRequest:
            pass
    elif d == "pb":
        state["s"] = state["s"][:-1]
        disp = state["s"]
        await callback.answer()
        try:
            await callback.message.edit_text(
                f"<b>Шаг 1: Номер телефона</b>\n\n"
                f"Вводи цифры только кнопками ниже.\n"
                f"Когда закончишь — нажми <b>✅</b>.\n\n"
                f"<b>Твой номер:</b> <code>+{disp or '...'}</code>",
                reply_markup=_p_kbd(disp),
            )
        except TelegramBadRequest:
            pass
    elif d == "pgo":
        raw = state["s"].strip()
        if not raw:
            await callback.answer("[x] Введи номер")
            return
        if not raw.startswith("+"):
            raw = "+" + raw
        if len(raw) < 8:
            await callback.answer("[x] Слишком короткий номер")
            return
        async with _auth_lock(uid):
            if uid not in auth_states or auth_states[uid].get("step") != "phone":
                return
            if not _allow_auth_attempt(uid):
                await callback.answer("Слишком много попыток. Подожди немного.", show_alert=True)
                return
            auth_states[uid]["step"] = "starting"
        await callback.answer()
        logger.info(f"phone: go {raw} for {uid}")
        await _start_auth(callback.message, uid, raw)


async def _start_auth(msg, uid: str, phone: str):
    # Повторная проверка: между connect_cb и этим вызовом мог измениться
    # allowlist (перезапуск) — не даём зайти через уже созданное состояние.
    if not session_allowed(uid):
        auth_states.pop(uid, None)
        await msg.answer("[x] Подключение недоступно.")
        return
    logger.info(f"_start_auth: creating client for {uid}")
    await asyncio.sleep(_rand_delay())
    client = TelegramClient(session_path(uid), API_ID, API_HASH)
    try:
        await client.connect()
        logger.info(f"_start_auth: connected for {uid}")
        result = await client.send_code_request(phone)
        h_full = result.phone_code_hash
        h = h_full[:5]
        logger.info(f"_start_auth: send_code_request OK phone=[+{phone[-4:]}] hash={h} timeout=None")
        auth_states[uid] = {
            "step": "code",
            "phone": phone,
            "client": client,
            "hash": result.phone_code_hash,
            "s": "",
            "_ts": time.time(),
        }
        logger.info(f"_start_auth: code requested for {phone} hash={h}...")
        await msg.answer(
            "[OK] <b>Запрос отправлен!</b>\n\n"
            "Сейчас на твой телефон в <b>Telegram</b> придёт push-уведомление с цифрами.\n\n"
            "[.] Открой Telegram на телефоне\n"
            "[.] Посмотри уведомление от самого Telegram (не от бота)\n"
            "[.] Цифры приходят как <b>всплывающее уведомление</b>\n\n"
            "Если ничего не пришло — проверь, что телефон онлайн и уведомления Telegram включены.",
        )
        await asyncio.sleep(_rand_delay())
        await msg.bot.send_message(
            chat_id=int(uid),
            text="<b>Введи цифры из уведомления:</b>",
            reply_markup=_d_kbd(),
        )
    except FloodWaitError as e:
        logger.error(f"_start_auth: FloodWait {e.seconds}s for {uid}")
        await msg.answer(f"[x] Подожди {e.seconds} сек.")
        await client.disconnect()
        auth_states.pop(uid, None)
    except PhoneNumberInvalidError:
        logger.error(f"_start_auth: invalid phone {phone} for {uid}")
        await msg.answer("[x] Не получилось. Проверь данные.")
        await client.disconnect()
        auth_states.pop(uid, None)
    except PhoneNumberOccupiedError:
        logger.error(f"_start_auth: phone {phone} occupied for {uid}")
        await msg.answer("[x] Этот номер уже используется в другом входе. Подожди и попробуй снова.")
        await client.disconnect()
        auth_states.pop(uid, None)
    except Exception as e:
        logger.error(f"_start_auth: error for {uid}: {e}", exc_info=True)
        await msg.answer("❌ Не удалось начать подключение. Попробуй ещё раз.")
        try:
            await client.disconnect()
        except Exception:
            logger.debug("_start_auth: disconnect after failure failed", exc_info=True)
        auth_states.pop(uid, None)


@router.callback_query(lambda c: c.data.startswith("d:") or c.data in ("ddel", "dgo", "_x"))
async def code_kb(callback: types.CallbackQuery):
    uid = str(callback.from_user.id)
    state = auth_states.get(uid)
    if not state or state["step"] != "code":
        await callback.answer()
        return
    d = callback.data
    if d == "_x":
        await callback.answer()
        return
    if d.startswith("d:"):
        digit = d.split(":", 1)[1]
        if len(digit) != 1 or not digit.isdigit():
            await callback.answer()
            return
        state["s"] += digit
        await callback.answer(f": {state['s']}")
        try:
            await callback.message.edit_reply_markup(reply_markup=_d_kbd(state["s"]))
        except TelegramBadRequest:
            pass
    elif d == "ddel":
        state["s"] = state["s"][:-1]
        await callback.answer()
        try:
            await callback.message.edit_reply_markup(reply_markup=_d_kbd(state["s"]))
        except TelegramBadRequest:
            pass
    elif d == "dgo":
        val = state["s"]
        if not val:
            await callback.answer("Сначала введи цифры")
            return
        async with _auth_lock(uid):
            if auth_states.get(uid) is not state or state.get("step") != "code":
                return
            if not _allow_auth_attempt(uid):
                await callback.answer("Слишком много попыток. Подожди немного.", show_alert=True)
                return
            state["step"] = "verifying"
        await callback.answer()
        phone_masked = state["phone"][-4:]
        logger.info(f"code: go phone=[+{phone_masked}] len={len(val)} hash={state['hash'][:5]} for {uid}")
        client = state["client"]
        try:
            await client.sign_in(state["phone"], val, phone_code_hash=state["hash"])
            logger.info(f"code: sign_in ok for {uid}")
            started = await _finish(uid, client, state["phone"], twofa=False)
            await callback.message.answer(
                "[OK] <b>Готово!</b>\n\n"
                "Команды работают во всех чатах.\n"
                "Одноразовые фото будут сохраняться."
                if started
                else "[x] Не удалось запустить сессию. Начни подключение заново: /start"
            )
        except SessionPasswordNeededError:
            logger.info(f"code: 2fa needed for {uid}")
            auth_states[uid]["step"] = "2fa"
            auth_states[uid]["s"] = ""
            auth_states[uid]["_ts"] = time.time()
            await callback.message.answer(
                "<b>[!] Облачный пароль (2FA)</b>\n\n"
                "Введи пароль от Telegram текстом в чат.\n"
                "Это тот же пароль, что ты вводишь в офиц. приложении."
            )
        except PhoneCodeInvalidError:
            logger.warning(f"code: invalid for {uid}")
            state["s"] = ""
            state["step"] = "code"
            await callback.message.edit_text(
                "[x] Не подошло.\n\n"
                "Проверь уведомление в Telegram на телефоне — там 5 цифр.\n"
                "Введи их ещё раз:",
                reply_markup=_d_kbd(),
            )
        except PhoneCodeExpiredError:
            logger.warning(f"code: expired for {uid}")
            await callback.message.answer(
                "[x] Время истекло. Начни заново — /start"
            )
            await client.disconnect()
            auth_states.pop(uid, None)
        except FloodWaitError as e:
            wait = getattr(e, "seconds", 0) or 0
            logger.warning(f"code: FloodWait {wait}s for {uid}")
            state["s"] = ""
            state["step"] = "code"
            state["_ts"] = time.time()
            await callback.answer(
                f"Подожди {wait} сек и введи код ещё раз.", show_alert=True
            )
        except Exception as e:
            logger.error(f"code: error for {uid}: {e}", exc_info=True)
            await callback.message.edit_text("❌ Не удалось проверить данные. Начни подключение заново.")
            await client.disconnect()
            auth_states.pop(uid, None)


def _is_twofa_message(msg) -> bool:
    """Фильтр 2FA-пароля: только личка с ботом в шаге 2fa.

    Пароль нельзя принимать из групп — иначе случайный текст или
    dot-команда из общего чата уйдёт в sign_in(password=...).
    """
    return bool(
        msg.text
        and msg.from_user
        and getattr(msg.chat, "type", None) == "private"
        and str(msg.from_user.id) in auth_states
        and auth_states[str(msg.from_user.id)]["step"] == "2fa"
    )


@router.message(_is_twofa_message)
async def twofa_handler(message: types.Message):
    uid = str(message.from_user.id)
    state = auth_states[uid]
    pw = message.text.strip()
    logger.info(f"2fa: uid={uid}")
    client = state["client"]
    async with _auth_lock(uid):
        if auth_states.get(uid) is not state or state.get("step") != "2fa":
            return
        if not _allow_auth_attempt(uid):
            await _erase_password(message)
            await message.reply("Слишком много попыток. Подожди немного.")
            return
        state["step"] = "verifying"
    # Сразу удаляем сообщение с паролем: он не должен оставаться в истории
    # чата (ни в облаке, ни у других участников — в личке это всё равно он сам).
    await _erase_password(message)
    try:
        await client.sign_in(password=pw)
        logger.info(f"2fa: ok for {uid}")
        started = await _finish(uid, client, state["phone"], twofa=True)
        await message.reply(
            "[OK] <b>Готово!</b>\n\n"
            "Команды работают во всех чатах.\n"
            "Одноразовые фото будут сохраняться."
            if started
            else "[x] Не удалось запустить сессию. Начни подключение заново: /start"
        )
    except PasswordHashInvalidError:
        logger.warning(f"2fa: wrong password for {uid}")
        state["step"] = "2fa"
        state["_ts"] = time.time()
        await message.reply("[x] Не подошло. Введи пароль ещё раз.")
    except FloodWaitError as e:
        wait = getattr(e, "seconds", 0) or 0
        logger.warning(f"2fa: FloodWait {wait}s for {uid}")
        state["step"] = "2fa"
        state["_ts"] = time.time()
        await message.reply(f"[x] Подожди {wait} сек и введи пароль ещё раз.")
    except Exception as e:
        logger.error(f"2fa: error for {uid}: {e}", exc_info=True)
        await message.reply("❌ Не удалось проверить пароль. Начни подключение заново.")
        await client.disconnect()
        auth_states.pop(uid, None)


async def _erase_password(message: types.Message) -> None:
    """Удалить сообщение с облачным паролем.

    Бот может удалять собственные сообщения в личке, а пароль 2FA — это
    полноценный секрет, который не должен валяться в истории переписки.
    Отказ удаления не критичен (например, слишком старое сообщение), поэтому
    ошибку глушим.
    """
    try:
        await message.delete()
    except Exception as e:
        logger.debug("session: could not delete 2FA password message: %s", e)


async def _finish(uid: str, client: TelegramClient, phone: str, twofa: bool = False):
    logger.info(f"_finish: uid={uid} twofa={twofa}")
    me = await client.get_me()
    await client.disconnect()
    auth_states.pop(uid, None)

    # Поднимаем клиент ДО записи, чтобы status отражал реальность: при неудаче
    # юзер не должен видеть «Готово» и «Включено».
    started = await telethon_manager.start_client(uid)

    # НО: sign_in уже создал валидный .session файл. Если записи в
    # user_sessions нет, `cleanup_orphan_sessions()` на следующем старте
    # удалит его как «сироту» — и юзеру придётся проходить аутентификацию
    # заново из-за сетевого сбоя. Поэтому при неудаче пишем status:
    # "pending" (не active ⇒ session_exists() == False ⇒ UI честен), но
    # файл защищён от удаления.
    status = "active" if started else "pending"
    user_sessions[uid] = {
        "phone": phone,
        "has_2fa": twofa,
        "status": status,
    }
    save_user_sessions()

    if not started:
        logger.error(
            "_finish: telethon start failed for %s — session saved as 'pending' "
            "(файл сохранён от cleanup, но команды не заработают до /logout+переподключения)",
            uid,
        )
        return False

    # Premium-кэш мог быть заполнен «False» до подключения — сбрасываем,
    # иначе юзер до 5 минут видит только unicode-эмодзи.
    invalidate_premium_cache(uid)
    logger.info(f"_finish: telethon started for {uid}")
    return True
