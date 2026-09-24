# Slim Bot — AGENTS.md

Telegram-бот на движке **Telethon** (MTProto, sole executor dot-команд в чатах). Пользователь подключает свой аккаунт через Telethon-сессию (auth flow в личке с ботом), чтобы команды работали в любом чате, сохранялись одноразовые фото, и резолвились произвольные `@username`. **aiogram** (Bot API) используется только для управления: личка с ботом (`/start`, auth-callbacks, inline-режим, private dot-команды).

Файлы в проекте читаются быстро: вся бизнес-логика в `handlers/`, всё in-memory хранилище и JSON-персистентность в `utils/storage.py`, весь Telethon-код (включая dot-команды, formatters и авто-перевод) в `utils/telethon_manager.py`. Подробности по каждой команде — в её handlers/commands/<name>.py + `_helpdb.py`.

---

## Архитектура

### 1. aiogram (Bot API, только личка с ботом) — `handlers/commands/`, `handlers/session.py`, `handlers/inline/`
- `@router.message()` — dot-команды и текст ТОЛЬКО в личном чате с ботом (в остальных чатах sole executor — Telethon).
- `session_router` — callback'и аутентификации Telethon-сессии (`c1`, `p:*`, `d:*`, и т.п.) + 2FA-text handler.
- `@router.inline_query()` — inline-режим (единая точка входа, см. §3).
- Файлы `handlers/business.py`, `handlers/messages.py`, `handlers/deletions.py` УДАЛЕНЫ (вместе с ними удалён весь Bot API-путь кэширования/view-once/удалений).

### 2. Telethon (MTProto, от имени пользователя) — `utils/telethon_manager.py`
- `events.NewMessage(outgoing=True)` → `_handle_outgoing` (dispatch dot-команд).
- `events.NewMessage()` → `_handle_incoming` (view-once media на входящих; spoiler намеренно НЕ сохраняется — это обычное медиа со скрытым превью, не самоуничтожающееся).
- `events.NewMessage(incoming=True)` → `_handle_auto_tr_incoming` (активный авто-перевод).
- Каждый пользователь со своей сессией → отдельный `TelegramClient`.
- Resolve `@username` / numeric chat_id для `.watch`/`.who`/inline-`@bot @user`.
- Приватный чат с ботом пропускается: `event.is_private and chat.bot → return`.

### 3. Inline-режим — `handlers/inline/`
- Единая точка входа `@router.inline_query()` в `__init__.py::dispatch_inline`. Это сознательное решение (high-priority fix code-review): в aiogram 3.x dispatcher параллельно прогоняет всех matched observers — несколько `@router.inline_query(...)` привели бы к двойному `inline.answer()` и `BadRequest`. Поэтому submodule'и (`help.py`, `status.py`, `profile.py`) экспортируют чистые async-функции, а не роутеры.

---

## Защита от двойной обработки

- `utils.storage.was_processed(chat_id, msg_id, thread_id=None)` возвращает `True`, если `(chat_id, msg_id, thread_id)` уже видели в последние 5 секунд; иначе регистрирует метку (ставит `_processed_msgs[key] = time.time()`) и возвращает `False`.
- TTL ключа **не продлевается при дублях**: повторный вызов в течение 5 секунд не сдвигает метку — окно guard идёт от первого появления сообщения.
- Cleanup — **ленивый**: при каждом вызове проверяется `now - _processed_last_cleanup > 5`; если да, удаляются все ключи старше 5 с и сдвигается «курсор». Полного фонового sweeper нет.
- Telethon **не** обрабатывает сообщения из личного чата с ботом (`_handle_outgoing` ранний return).
- `was_processed(chat_id, event.id / msg_id, thread_id)` проверяется в Telethon `_handle_outgoing`/`_handle_incoming` до любой обработки, а также в `love.py` для private dot-команд в личке с ботом.
- Thread-aware: ключ `(chat_id, msg_id, thread_id)` — guard срабатывает только в топике, не cross-topic.

---

## Роутеры и приоритет

### aiogram (`bot.py`)
Очередность строго зафиксирована:
1. `commands_router` — все dot-команды + `/start /help /status /logout` (только личка с ботом).
2. `inline_router` — единственный `@router.inline_query()`.
3. `session_router` — callback'и аутентификации (`c1`, `p:*`, `d:*`, и т.п.).
4. `errors_router` — глобальный fallback.

### Commands (`handlers/commands/__init__.py`)
Полная точная последовательность `include_router(...)`:
```
cmdhelp → dm → extra → help → id → love → ping → start
       → status → time → timezone → watch → logout → tools
       → netcmds → admins → pin → coin → knowledge → ai → opencode
```
- `cmdhelp` подключён **первым**, чтобы `.cmd справка` (`.cmd help` / `.cmd ?` / `.cmd хелп`) обрабатывался до основных роутеров. (`is_help_request` + ключи из `_helpdb.ALIASES`).
- В этом же файле есть два `@router.message(...)` **fallback'а**:
  - `ignore_unauthorized` — для юзеров БЕЗ Telethon-сессии: всё ещё пытается выдать подсказку через `suggest_text`.
  - `suggest_unknown_private` — для юзеров С сессией, в личке с ботом (Telethon не достаёт сюда): всплывает подсказка по Левенштейну.
- Telethon-only модули (`admins`, `pin`, `invitelink`, `delmsg`, `dm`, `tagall`) не имеют aiogram Router — вызываются напрямую из `telethon_manager._handle_outgoing`.

---

## Аутентификация Telethon (`handlers/session.py`)

Поток полностью проходит через callback'ы и inline-кнопки внутри личного чата с ботом (никакого ручного `/register`):

```
1. `[+] Включить`  → callback_data: c1
                     → auth_states[uid] = {"step":"phone", "s":""}
                     → показать цифровую клавиатуру p_kbd

2. Цифры + `✅`     → callback_data: p:X, pb, pgo
                     → добавляем 'X' к s; префикс '+' ставится автоматически
                     → pgo → _start_auth: создаётся TelegramClient, send_code_request(phone)

3. Код push'ом     → callback_data: d:X, ddel, dgo
                     → state: code, показываем d_kbd
                     → dgo → client.sign_in(phone, code, phone_code_hash)

4. 2FA (если есть) → text-input в личке, авто-фильтр step=="2fa"
                     → client.sign_in(password=pw)

5. _finish: сохраняем user_sessions[uid], запускаем TelethonManager.start_client(uid)
```

### Обфускация от паттернов Telegram
- В тексте: «код/сессия/Telethon» → «цифры/проверь уведомления/дополнительные возможности/обновление».
- `send_code_request` возвращает push в Telegram (НЕ SMS). `force_sms=True` удалён из Telegram API и НЕ ИСПОЛЬЗУЕТСЯ.
- Кнопки цифровых клавиатур идут с задержкой `0.3 + rand(0..2.0)` секунд (`secrets.randbelow(2000)/1000`, итого 0.3–2.3 c) между шагами. Это всё в `handlers/session.py::_rand_delay`.
- FloodWaitError, PhoneCodeInvalidError, PhoneCodeExpiredError, PhoneNumberInvalidError, PhoneNumberOccupiedError обрабатываются индивидуально.

### Cleanup инфраструктура
- `cleanup_orphan_sessions()` (вызывается из `bot.py::on_startup`): сканирует `sessions/` и удаляет `.session`/`-journal`, у которых нет соответствующей записи в `user_sessions.json`. Хеш пути определяется как `sha256(uid)[:16]` — функция `utils.storage.session_path(uid)`.
- `auth_state_cleaner()`: фоновая задача в event loop, раз в 60 секунд чистит `auth_states` старше `AUTH_STATE_TTL = 300 c` (5 мин); для каждого отключает `TelegramClient` через `disconnect()`.
- `logout(uid)` в `TelethonManager`: `client.log_out()` (revoke на стороне Telegram), затем удаление `.session`/`.session-journal`, удаление из `user_sessions`, чистка `_tr_locks` per-user.

### Callback data для аутентификации
| Значение | Назначение |
|---|---|
| `c1` | Начать подключение (ввод номера) |
| `w1` | Показать объяснение «зачем это» |
| `b1` | Вернуться к справке из объяснения |
| `p:X` | Цифра для номера (`X` = `+` или `0`–`9`) |
| `pb` | Бэкспейс для номера |
| `pgo` | Подтвердить номер (вызывает `_start_auth`) |
| `d:X` | Цифра для кода (`X` = `0`–`9`) |
| `ddel` | Бэкспейс для кода |
| `dgo` | Подтвердить код (вызывает `client.sign_in`) |
| `_x` | Пустышка (заглушка на месте плейсхолдера `✅`) |
| `logout_ask` | Показать подтверждение выхода |
| `logout_yes` | Отозвать сессию (`TelethonManager.logout`) |
| `logout_no` | Отменить подтверждение |

---

## Slash-команды (только в личке с ботом)

| Команда | Описание |
|---|---|
| `/start` | Статус Telethon-сессии + кнопки. Если сессия — показать `[x] Выключить`, иначе `[+] Включить`. |
| `/help` | Справка по всем командам (`format_help(has_session)`). |
| `/status` | Статус Telethon-сессии: «подключена» / «не подключена». |
| `/logout` | Кнопка подтверждения отзыва Telethon-сессии (см. callback'ы выше). |

---

## Dot-команды

Dot-команды работают в ДВУХ каналах:
- `@router.message()` (только личка с ботом).
- Telethon `outgoing=True` (все остальные чаты, sole executor там).

В таблице ниже «aiogram + Telethon» означает: aiogram-ветка работает только в личке с ботом, Telethon-ветка — во всех остальных чатах.

Если у юзера активна Telethon-сессия, aiogram dot-роутеры с `session_exists(uid) → return` пропускают событие, чтобы отдать приоритет Telethon-обработчику. Исключение: Telethon-only команды идут строго через `_handle_outgoing`, поэтому aiogram-роутеров для них нет вообще.

| Команда | Алиасы | Описание | Движок |
|---|---|---|---|
| `.ping` | `.пинг` | Задержка между сообщением и его обработкой | aiogram + Telethon |
| `.time` | `.время` | Текущее время с учётом `.timezone` настройки (по умолчанию UTC) | aiogram + Telethon |
| `.id` | `.инфо` | Chat ID + User ID + Topic ID + username + имя | aiogram + Telethon |
| `.me` | `.я` | Краткая инфа о себе + телефоне + Premium + bio (через Telethон) | aiogram + Telethon |
| `.chat` | `.чат` | Краткая инфа о чате + тип + verified/scam/restricted + bio (Telethon) | aiogram + Telethon |
| `.who` | `.кто` | (см. ниже) | aiogram + Telethon |
| `.love` | `.любовь` | Анимированное сердечко (3 цикла по 7 цветов, 0.45 с шаг) | aiogram + Telethon |
| `.coin` | `.монетка`, `.монета`, `.орёл`, `.решка` | Случайное «орёл»/«решка» | aiogram + Telethon |
| `.watch` | `.следить` | Добавить чат/топик в отслеживание | aiogram + Telethon |
| `.unwatch` | `.хватит`, `.забыть` | Убрать чат/топик | aiogram + Telethon |
| `.watched` | `.список` | Список отслеживаемых: id/title как кликабельные ссылки + топик, если есть | aiogram + Telethon |
| `.help` | `.помощь` | Полная справка | aiogram + Telethon |
| `.net` | `.сеть`, `.сет` | Автоматический анализ IP, домена или URL: DNS, Geo/ASN, TLS, редиректы и пассивный аудит связанных IP | aiogram + Telethon |
| `.hash` | `.хеш`, `.хэш` | md5/sha1/sha224/256/384/512/sha3_256/512/blake2b/blake2s — текста или файла по reply | aiogram + Telethon |
| `.uuid` | `.юид` | До 20 UUIDv4 за раз | aiogram + Telethon |
| `.b64` | `.base64` | base64 encode (`encode`/`e`/`enc`) или decode (`decode`/`d`/`dec`) | aiogram + Telethon |
| `.timezone` | `.таймзона`, `.tz` | Установить TZ для `.time` (см. ниже) | aiogram + Telethon |
| `.tr` | `.перевод`, `.пер` | Перевод текста (aiogram + Telethon). Подкоманды `.tr auto/stop/list` — только Telethon | aiogram + Telethon |
| `.calc` | `.калк` | AST-eval калькулятор: `+ - * / // % ** & \| ^ << >>` и функции `sin/cos/atan/log/.../hex/bin/oct` | aiogram + Telethon |
| `.save` | `.сохранить` | Переслать replied сообщение в Избранное. aiogram-проверка сессии → delegate в Telethон | Требует Telethon-сессии |
| `.ии` | `.ai`, `.ии?` | Запрос к LLM (см. ИИ-раздел ниже) | aiogram + Telethon |
| `.del` | `.удалить` (`N` 1..100) | Удалить саму команду + ещё N последних СВОИХ сообщений в чате (то есть `.del` = N+1 удалённых) | Telethon only |
| `.tagall` | `.тегвсех`, `.все` | Тегнуть всех (до 50) в группе через mentions | Telethon only |
| `.влс` | `.vls`, `.лс`, `.dm` | Отправить текст (по умолчанию `.`) автору replied сообщения в ЛС; затем удаляет команду | Telethon only |
| `.admins` | `.админы` | Список администраторов чата (с 👑 для owner) | Telethon only |
| `.pin` | `.закрепить`, `.закреп` | Закрепить replied сообщение (или саму команду). Флаг `silent\|тихо\|no\|ns\|loud\|громко\|n` (по умолчанию silent) | Telethon only |
| `.unpin` | `.открепить`, `.раскрепить`, `.откреп` | С reply — указанное, без — последнее закреплённое, `all`/`все` — снять все | Telethon only |
| `.invitelink` | `.ссылка`, `.инвайт`, `.invite` | Получить/создать инвайт-ссылку (нужны права админа) | Telethon only |
| `.cmd справка` | `.cmd help`, `.cmd ?`, `.cmd хелп` | Per-command help из `_helpdb.HELPS`. aiogram: `reply` (private). Telethon: `event.edit`. Подключён ПЕРВЫМ роутером в `handlers/commands/__init__.py`. | aiogram + Telethon |
| `.quote` | `.цитата` | Красиво оформить replied сообщение **В ВИДЕ PNG-картинки** (utils/quote_image.py: Pillow + DejaVu Sans, ~1200px шириной): header + body + signature в красивом layout. Если Pillow/font недоступен — graceful fallback на HTML blockquote. Telethon only |
| `.ня` | — | Включить/выключить **catgirl-rewrite** исходящих сообщений юзера в ТЕКУЩЕМ чате. Telethon-only потому что перехват исходящих нужен через MTProto (`aiogram Bot API` бот не может читать/редактировать чужие исходящие). После toggle ON все не-dot сообщения юзера в этом чате получают procedurally-rewritten в стиле «кошко-девочка» (заикание первой согласной + удвоение гласной в середине слова + `*action*`-префикс из пула + каомодзи-суффикс из пула). Dot-команды (`.id`, `.ping`, `.tr`, …) **никогда не редактируются** — это снимает гонку между hook'ом обработчика команды и rewriter'ом. Подкоманды: `.ня стоп`/`.ня выкл`/`.ня off`/`.ня stop` → OFF; `.ня список`/`.ня list` → список активных чатов. Storage: `utils/storage.py::nya_chats` (отдельный файл `nya_chats.json`). Tunables: `NYA_STUTTER_PROB=0.35`, `NYA_VOWEL_DOUBLE_PROB=0.25`, `NYA_ACTION_PROB=0.30`, `NYA_KAOMOJI_PROB=0.40`, `NYA_MIN_LEN=4` (config.py). Rewriter: `utils/catgirl.py::to_catgirl(text) → str`. Telethon-side: `telethon_manager._handle_outgoing` — early-return для НЕ-dot сообщений проверяет `is_nya_chat(uid, chat_id)` и запускает `asyncio.create_task(self._apply_nya(...))` (см. ниже). | Telethon only |
| `.вгф` | `.vfg` `.gif` | Reply на **photo** → **статичная GIF** через Pillow (2 кадра по 5с с 1-пиксельным LSB-flip patch между ними — обход Pillow bug, который cxлопывает 100% identical кадра даже при `optimize=False`; визуально неразличимо, технически byte-diff для Telegram anti-static-doc heuristic, infinite loop, **без zoom/wobble** — статично, utils/gif_converter.py::`photo_to_gif_bytes`: RGBA-flatten + GIF89a save + sub-pixel nudge, max_size=1600×1600 clamp). Reply на **video / video_note / анимированное фото** → **анимированная GIF** через ffmpeg pipeline (utils/gif_converter.py::`video_to_gif_bytes`: одно-проходный `fps=18, scale=720:-2:flags=lanczos, split[s0][s1]; [s0]palettegen=stats_mode=full[p]; [s1][p]paletteuse=dither=sierra2_4a`, bounds `max_duration=8с × max_fps=18 × max_width=720`, audio stripped через `-an`, hard timeout 60с против зависаний). Оба пути отправляют `send_file` с `DocumentAttributeAnimated()` + `mime_type="image/gif"` → Telegram рендерит inline-playable preview независимо от того, motion это или статика. Стикеры и прочее non-media — reject. Telethon only |

### Движок aiogram-only / Telethon-only — детали

- `.save` в `aiogram`-пути проверяет `session_exists`, после чего дергает `telethon_manager.get_client(uid).forward_messages("me", ...)`. Альтернативно (через Telethon) — отдельный `_handle_save(event)`.
- `.del` (`delmsg.handle`):
  - По умолчанию N=1, потолок `max(1, min(n, 100))`.
  - Команда сама (`event.id`) ВСЕГДА попадает в список удаления — `to_del = [event.id]` — и потом добирается через `iter_messages(..., from_user=me.id, limit=n+50)`.
  - Ошибки: `MessageDeleteForbiddenError`, `FloodWaitError` (возвращает секунды), общие Exception.
- `.tagall` (`tagall.handle`):
  - Доступ к participants через `client.get_participants(chat_id, limit=100)` (Telethon high-level API).
  - Из первых 100 берётся максимум 50 mentions (`participants[:50]`) — это hard-cap Telegram на количество упоминаний в одном message, остальные молча теряются.
  - Боты (`p.bot`) и удалённые (`p.deleted`) аккаунты пропускаются. Чанки по 5 в строке (для удобства чтения).
- `.влс` (`dm.handle`):
  - Без текста отправляет «.» по умолчанию. После успешной отправки пытается удалить исходную команду (`event.delete()`).
  - Резолв через `client.get_entity(sender_id)`.
- `.pin` / `.unpin` (`pin.handle`): используют raw TL `UpdatePinnedMessageRequest` и `UnpinAllMessagesRequest` напрямую (не high-level методы). `_get_pinned_ids` обходит `iter_messages(pinned=True, limit=50)` для получения top-down списка.
- `.admins`: идёт через `iter_participants(filter=ChannelParticipantsAdmins)`; резолв senior owner'а — `admin_rights.is_creator`. `ChatAdminRequiredError` для админских операций.
- `.invitelink`: пытается создать новую (`ExportChatInviteRequest`) или получить существующую (`GetExportedChatInvitesRequest`). Если обе ветки падают — выводит `type(e).__name__`.

### `.who` — два режима

- **aiogram**: только личка с ботом и только БЕЗ Telethon-сессии (при активной сессии `session_exists → return`, приоритет отдаётся Telethon). Требует reply; показывает `_make_who_aiogram` (Bot API-поля: язык, premium, bot-флаги: `can_join_groups`, `can_read_all_group_messages`, `supports_inline_queries`).
- **Telethon** (`utils/telethon_manager._handle_outgoing`):
  - `.who @u1 @u2 @u3` — Multi-targets (до 5). Делает `event.edit(progress)`, затем для каждого target резолв через `get_entity(timeout=10)` + `GetFullUserRequest(timeout=10)` + скачивание аватарки через `download_avatar_bytes` (magic-byte detect `jpg/png/gif/webp`). Каждый результат шлёт в чат как photo+caption (если есть фото) или просто caption. В конце — итоговый edit summary с числом resolved/failed (3 первых failures в summary, остальное «…и ещё N»).
  - `.who` (без аргументов, требует reply) — `_who_with_reply` (race-free flow): пытается `send_file/send_message` с reply_to и timeout=30; на FloodWait — `event.edit("[x] FloodWait{s}c")`; на Timeout — НИЧЕГО не делает (telethon мог частично отправить file_id); на других Exception — fallback `event.edit(text)`. После успешного send — best-effort `event.delete()` чтобы скрыть команду. Старый дефект (delete-before-send race) исправлен.
  - Аватарка: AI-карточка имеет `phone`, `lang_code`, `premium`, `verified`, `scam`, `fake`, `restricted`, `support`, `deleted`, `mutual_contact`, `contact`, `status` (online/offline/time), плюс `bio` через `full_user.about` (truncate до 200 символов, перевод `\n` в пробел). HTML-escape критичен для всех user-controlled полей (`_esc()`).

### `.watch` — поведение и фото-режимы
- **Без Telethon-сессии**: работает в чате, где юзер сейчас находится. В форуме — добавляет chat+thread. Без аргументов в личке с ботом: thread=0 (т.е. «весь чат»).
- **С Telethon-сессией**:
  - `.watch` в чате — текущий топик (или весь чат, если не форум).
  - `.watch @username` или `.watch -100...` — резолв через `telethon_manager.resolve_entity` → ВСЕГДА thread=0 (весь чат).
- При добавлении/удалении чата автоматически корректируется `photo_settings` для этого юзера (`_auto_enable_photos`, `_disable_photos_for_chat`):
  - Если фото выключены (`enabled:false`) → включить в режим `only_selected`, добавить `[chat, thread]` в exceptions.
  - Если `only_selected` → добавить в exceptions.
  - Если `all_except` → убрать из exceptions.
  - Если `all` (без исключений) → ничего.
- `unwatch` делает обратное действие. `_photo_note(user_id)` дописывает «Одноразовые фото будут/не будут сохраняться».

### `.tr auto/stop/list` — Telethon-only
- `.tr auto` — включает авто-перевод в текущем чате (без `event.is_private`).
- `.tr stop` — выключает авто-перевод в текущем чате.
- `.tr list` — список чатов с включённым auto (названия резолвятся через `get_entity(timeout=5)`, HTML-escape защищает от инъекций).
- Состояние хранится в `auto_tr_chats.json` → `auto_tr_chats: dict[str, list[int]]`.
- `_handle_auto_tr_incoming` фильтры:
  - Чат должен быть в `auto_tr_chats[user_id]`.
  - Текст имеет ≥3 буквенных символов.
  - Не бот, не своё сообщение, не начинается с `./!/` (не команда).
  - Per-`(user_id, chat_id)` `asyncio.Lock` + `asyncio.sleep(0.5)` сериализует ответы. На Google-определение `ru` — не отвечает.
- На ошибку перевода таймаут `12 c` через `asyncio.wait_for`; ловит всё подряд, тихо выходит.

### `.timezone [значение]`

- Форматы, которые `utils.timezones._canonicalize` принимает:
  - Оффсеты: `+3`, `-5`, `+5:30`, `+14` (валиден), с минутами.
  - IANA: `Europe/Moscow`, `Europe/Kyiv`, `Asia/Tokyo`, ...
  - Русские алиасы (см. константу `RU_ALIASES`): `мск`, `спб`, `питер`, `москва`, `киев`, `минск`, `алматы`, `тбилиси`, `ереван`, `баку`, `дубай`, `стамбул`, `нью-йорк`, `лос-анджелес`, `чикаго`, `токио`, `пекин`, `сеул`, `дели` и т.д.
  - Сброс: `UTC`, `0`, `reset`, `сброс` (case-insensitive) → возвращает canonical `"0"`.
- Без аргументов — показывает текущее значение + список пресетов (TZ_PRESETS: Москва→Камчатка, Лондон/Берлин/Киев, НЙ/ЛА/Чикаго/Токио, …).
- Применяется **немедленно** к следующему `.time`. `"+3"` хранится компактно (только нужная точность). IANA — строкой.

### `.cmd справка` — per-command help
- Триггер: в любой команде второе слово — одно из `справка`, `help`, `?`, `хелп`. Примеры: `.ping справка`, `.tr help`, `.watch ?`.
- `_helpdb.ALIASES` хранит карту `alias → canonical_key`. `is_help_request` парсит `\.<cmd> <word>`.
- Подключён **первым** роутером (`include_router(cmdhelp.router)`), чтобы ответ пришёл «на месте» (`reply()` для private; Telethon: `event.edit`).

### Подсказки при опечатках (`utils.suggest`)
- Алгоритм: Левенштейн по `_PAIRS` (английский canonical + русские алиасы). `max_distance = 2`. Early-return `\==1`.
- Индекс собирается **один раз** (`_index_cache: dict[str, str] | None`) при первом вызове, дальше кэшируется.
- Возвращает английский canonical (например, `.ping`), не оригинальный алиас юзера.
- Три места вызова:
  1. `handlers/commands/__init__.py::ignore_unauthorized` — для неподключённых юзеров в личке.
  2. `handlers/commands/__init__.py::suggest_unknown_private` — fallback для подключённых в личке.
  3. `utils/telethon_manager._handle_outgoing` ELSE-ветка — для всех Telethon-чатов (без private-bot чата).

---

## Inline-режим

В любом чате: `@<bot_username> <запрос>`. Dispatcher в `handlers/inline/__init__.py::dispatch_inline` выбирает submodule по тексту.

| Запрос | SUBMODULE | Что показывает | cache_time |
|---|---|---|---|
| `@bot` (пустой) | `help.py` | Главная карточка-обзор | 300с (авторизован), 30с (нет) |
| `@bot помощь` / `.help` / `.справка` / `.h` / `.?` | `help.py` | Справка по inline | то же |
| `@bot статус` / `.status` | `status.py` | Live-карточка: uptime, ping (`bot.get_me()`), активные Telethon-клиенты, сессии | 15с (авторизован), 10с (нет) |
| `@bot @u1 @u2 ...` (до 5) | `profile.py` | Карточка профиля через Telethon для каждого target | 300с single, 180с multi |
| ничего из выше | — | Пустой `inline.answer(results=[...], is_personal=True)` | 60с |

Кнопка «Подключить бота» на unauthorized-ветках: `InlineQueryResultsButton(start_parameter="start")` через `handlers/inline/__init__.py::connect_button()`.

### Profile multi-target (`@bot @a @b @c`)
- Парсер `parse_inline_targets(query, limit=5)` — split по `\s+,`, дедупликация case-insensitive, `lstrip('@')`.
- Если нет Telethon-клиента — пустой `results`. Если `!session_exists(uid)` → «Нужен Telethon» + connect_button (авторизация в inline = `session_exists`, отдельного `connected_users` больше нет).
- Если `telethon_manager.get_client(uid)` есть — для каждого target: `get_entity(timeout=10)` → `GetFullUserRequest(timeout=10)` → `_format_profile(entity, full_user)`.
- Все user-controlled поля экранируются через `html.escape`-аналог `_esc()` (`&<> → &amp;&lt;&gt;`). Это FIX H2 из code-review — обязательно.
- Profile показывает: тип (User/Chat), ID, имя, активные usernames (collectibles из `full_user.usernames` + legacy `entity.username`), телефон, язык, premium, verified/scam/fake/restricted, bio, `common_chats_count` (только для User'а).
- Если в кэше ответ появится как несколько articles — каждый со своим `id="profile_<entity_id>"`.

### Status (`@bot статус`)
- Все счётчики (`active_clients`, `sessions_total`) считаются в runtime через прямой доступ к `telethon_manager._clients`, `user_sessions`.
- Пинг — round-trip `bot.get_me()` через `time.monotonic()`. `is_personal=True` потому что статус на каждого юзера.

---

## Форумы (forum-каналы / topics)

### Идентификация топика
- **aiogram**: `message.message_thread_id`. `None`/`0` → не форум (или General).
- **Telethon**: `utils.telethon_manager.thread_id_of(event)`:
  1. Берёт `event.message.reply_to`.
  2. Если `reply_to.forum_topic=True` — пробует `reply_to_top_id` (корень топика), иначе `reply_to_msg_id`.
  3. Fallback: `reply_to_top_id` (для ответов внутри топика в обычном супергруппе).
- `telethon_reply_to(event)` — для `event.respond(...reply_to=...)`: prefers `reply_to_top_id`, fall back на `reply_to_msg_id`. Это удерживает ответ **внутри** того же топика, что важно для форумов.
- `_topic_name_async(client, chat_id, thread_id)` — пытается подтянуть имя топика через `GetForumTopicsRequest(channel=chat_id, offset_topic=thread_id, limit=1)`. Используется в `_handle_incoming` для caption'а view-once.

### `watched_chats.json` — формат с топиками
- `watched_chats.json`: `user_id → [[chat_id, thread_id], …]`.
  - `thread_id = 0` — весь чат (все топики + General).
  - `thread_id = N` — только этот топик. `is_chat_watched` вернёт `True` если в списке есть именно `[chat_id, N]` ИЛИ `[chat_id, 0]`.
- При загрузке есть авто-миграция старого плоского формата `[int, int]` → `[[int, 0]]` (`storage._migrate_watched`).

### Поведение `.watch` (см. также таблицу выше)
- В форум-чате без аргументов — добавляет текущий топик (chat+thread).
- `.watch @username` / `.watch -100...` (Telethon) — ВСЕГДА thread=0 (весь чат).
- `.unwatch` без аргументов в форуме — снимает только этот топик.

### Ответы в тот же топик
- **aiogram**: `handlers/commands/_base.py::thread_kwargs(message)`, `reply_in_topic()`, `answer_in_topic()` автоматически пробрасывают `message_thread_id` в `send`-методы. Возвращают `{}` при thread≤0 (тогда Telegram сам кладёт сообщение в General).
- **Telethon**: в командах с `event.respond/send_message` явно передаётся `reply_to=telethon_reply_to(event)` — Telethon сам кладёт ответ в текущий топик.

### Save view-once в форумах
- `_handle_incoming` (Telethon): проверяет `is_chat_watched(uid, chat_id, thread_id)` и `is_photo_allowed(uid, chat_id, thread_id)` — обе функции опционально принимают thread_id.
- В caption уведомления: `Топик: <b>{topic_name}</b>` если Telethon смог достать имя через `_topic_name_async` (иначе fallback — id).
- `download_view_once` идёт по `chat_id + ids=msg_id` — Telethon сам резолвит топик, `thread_id` нужен только для логов.

---

## Отслеживание удалённых сообщений — УДАЛЕНО

Фича удалена вместе с удалением Bot API-пути кэширования: кэш сообщений (`message_cache`, `cache_message`, `find_cached_messages*`, `CACHE_SIZE`, `DATA_FILE` в config) удалён из `utils/storage.py`, файлы `handlers/messages.py` / `handlers/deletions.py` / `handlers/business.py` удалены. Восстановление удалённых сообщений больше не поддерживается.

---

## Сохранение одноразовых / спойлер-фото

Триггер (только Telethon):
- `_handle_incoming`: `media.ttl_seconds > 0`. Spoiler (`media.spoiler is True` без TTL) намеренно пропускается — это не view-once.

Поток:
1. `tid = thread_id_of(...)`.
2. `session_exists(uid) AND is_chat_watched(uid, chat_id, tid) AND is_photo_allowed(uid, chat_id, tid)`.
3. `was_processed(chat_id, msg_id, tid)` — guard от двойной работы.
4. `telethon_manager.download_view_once(uid, chat_id, msg_id, thread_id=tid)`.
5. `TEMP_DIR / f"{uid}_{msg_id}_{int(time.time())}{ext}"` — расширение из MIME (`.mp4` если `video/*`, `.gif` если `gif`, иначе `.jpg`).
6. `client.download_media(msg, file=path)`.
7. Через `telethon_manager._bot.send_photo` / `send_video` отправляет юзеру в личку ОТ БОТА, потом `os.remove(path)`.

### Настройки `photo_settings`

```json
{
  "8002855167": {
    "enabled": true,
    "mode": "only_selected",
    "exceptions": [[-1001234567890, 0], [-1001234567890, 42]]
  }
}
```

- `enabled: false` — сохранение полностью выключено.
- `mode: "all"` — `True` для любых чатов/топиков; **`exceptions` игнорируются** полностью (в отличие от `all_except`).
- `mode: "all_except"` — `True` везде, КРОМЕ чатов/топиков из `exceptions`.
- `mode: "only_selected"` — только если `(chat_id, tid)` ИЛИ `(chat_id, 0)` есть в exceptions.
- `exceptions` элементы нормализуются: `[chat_id, thread_id]` для двух-арг. записей, иначе `[chat_id, 0]`.

По умолчанию: `enabled: false`. Авто-включение в режиме `only_selected` происходит при **первом** `.watch`, если у юзера есть Telethon-сессия. См. `watch.py::_auto_enable_photos`.

---

## ИИ (команда `.ии` / `.ai` / `.ии?`)

- Модель: `gpt-5.6-luna` через Willow API, effort `low`.
- Endpoint: `https://opencode.ai/zen/v1/chat/completions` (OpenCode Zen, `utils/ai.py`).
- Vision: если передано `image_bytes` — последний user-message становится multimodal (`{type: text, type: image_url}` с `data:image/{mime};base64,…`).
- **Системный промпт и все inline-шаблоны** живут в `utils/ai_prompts.py` (отдельный Python-модуль — редактировать без править handlers/commands/ai.py). `utils.ai.SYSTEM_PROMPT` — back-compat алиас на `ai_prompts.SYSTEM`.
  - Только Telegram-HTML-теги: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<a href="…">`.
  - **Запрещены**: `<pre>`, `<blockquote>`, triple-backtick блоки, любой markdown (`**, *, __, _, `, ```, #`).
  - Многострочный код — через `<code>с&nbsp;\nпереносами</code>` внутри одного тега.
- Параметры запроса: `temperature: 0.5`, `max_tokens: 4000`, `stream: False`, timeout 60 c.
- **`utils/ai.py::ask()` теперь принимает `include_user_message: bool = True`** — если False, функция НЕ добавляет финальный user-turn. Это нужно для `_llm_iterate`, где caller сам накапливает messages (иначе при round 1+ original user question терялся из контекста).

### Финальный формат ответа
- HTML-вывод обёрнут в `<blockquote>` (визуальная консистентность с .ping/.time/.id).
- Под блоком — `<i>` footer с хинтами `[память: N] · [использовал: tool1, tool2]`.
- При `.ии дебаг` — дополнительный `<blockquote>` со списком тулов (R{n}.{i} .{cmd}{args} → status).
- Всё строится через `_format_ai_response(...)` в handlers/commands/ai.py.

### Режимы
- `.ии вопрос` — простой вопрос с памятью диалога. **Фикс `_parse_query`**: для `.ии foo bar baz` возвращается `"foo bar baz"` целиком (раньше терялось всё кроме первого слова).
- `.ии ctx=N вопрос` (или `context_len=N`) — user-facing override размера локального контекста. Собирает ±N сообщений вокруг центра (`reply.id` если есть реплай, иначе `event.id`). Диапазон `[0, AI_CONTEXT_LEN_MAX]` (default 0..200), default — `AI_CONTEXT_LEN_DEFAULT` (20). При `ctx=0` локальный контекст пустой (только реплай/вопрос + deep walk + memory), при `ctx=100` — расширенный сбор для длинных переписок. Telethon-only: aiogram-путь тихо проглатывает флаг (нет MTProto), `_parse_query` всё равно вырезает его из текста чтобы LLM не получила мусор в своём user-turn. Парсинг regex `r"\b(?:context_len|ctx)=(\d+)"` (case-insensitive) — берётся ПЕРВОЕ вхождение где угодно в строке. Hint в footer'е: `[конт: ±N]` показывается только при N != default (через `fmt_footer_ctx`).
- `.ии` (reply на текст) — короткий комментарий.
- `.ии запрос` (reply на текст) — вопрос + контекст = replied message + ~20 соседних через Telethon (`_collect_context_safe`, `min_id=reply.id-count`, `max_id=reply.id+1`, `limit=count`).
- `.ии` (reply на фото) — vision-анализ. Сначала photo/document с `image/*` mime / НЕ-анимированный WEBP-стикер скачивается. Анимированные (TGS=`application/x-tgsticker`) и WebM-видео-стикеры ignored.
- `.ии сброс` — очистить память текущего чата (`ai_memory.clear(uid, cid)`). Возвращает `<size> сообщений`.
- `.ии сброс все` / `.ии сброс all` — очистить **все** диалоги юзера (`ai_memory.clear_all(uid)`). Возвращает `<count> диалогов`.

### Self-tool-use
- LLM может в ответе попросить бота выполнить read-only команду через синтаксис `<tool_use>cmd args</tool_use>`.
- **Переезд с `++TOOL:.cmd args++` на `<tool_use>cmd args</tool_use>`** — XML/HTML-подобный синтаксис, привычнее для LLM и снижает риск что «кто я?» будет «отвечен из контекста» вместо вызова `<tool_use>me</tool_use>`. Whitelist фильтрует в `_llm_iterate` через `AI_TOOL_ALLOWLIST` (config.py).
- Regex парсера: `r"<tool_use>([\s\S]*?)</tool_use>"` — multiline OK, case-insensitive (LLM иногда пишет `<Tool_use>`). Парсинг cmd/args делает `_parse_tool_call` — strip + split(maxsplit=1), точка-префикс принимается и убирается (`.me` → `me`).
- **Hard switch для АВТОЗАПУСКА**: legacy `++TOOL:.cmd args++` НЕ приводит к выполнению команды (`_TOOL_RE` ловит только `<tool_use>…</tool_use>`, `_extract_tool_calls` тоже использует только его). Но `_sanitize_user_content` И `_strip_tool_calls` дополнительно прогоняются через `_LEGACY_TOOL_RE`, чтобы защититься от миграционных утечек: если исторический user-turn в `ai_memory` содержит `++TOOL:...++` (написан ДО миграции), LLM может его сэмулировать в ответе, и тогда этот сырой сентинел должен быть вырезан из user-visible текста и из записываемой в память истории. Это именно дефанг, не запуск.
- **Orphan/unclosed `<tool_use>`**: если LLM забудет закрывающий тег (типичная LLM ошибка), regex `finditer` не найдёт пару, `_strip_tool_calls` ничего не удалит, юзер увидит сырой `<tool_use>...`. `SYSTEM_TOOLS` явно требует ВСЕГДА закрывать `<tool_use>cmd args</tool_use>` — это часть поведения, не конфигурируется.
- **Явные правила когда звать тулы** (в `utils/ai_prompts.py::SYSTEM_TOOLS`):
  - вопросы про самого юзера (`кто я / мой id / мой premium / мой язык / мой bio / я Premium?`) → `<tool_use>me</tool_use>`, НЕ угадывай из истории чата или ai_memory (там могли писать другие люди или старые данные);
  - вопросы про текущий чат (`что это за чат / название / тип / топик`) → `<tool_use>chat</tool_use>`;
  - конкретный `@user` → `<tool_use>who @user</tool_use>`;
  - что-то обсуждалось в чате, но в локальном контексте ты не видишь → `<tool_use>regex keyword</tool_use>`;
  - вопросы о модели/платформе/факты/понятия — ПРЯМО из своих знаний.
- Результаты тул приходят в формате `[+].cmd args: <result>`, на ошибку — префиксы `[bad-pattern]` / `[no-match]` / `[error]` / `[rejected]` / `[usage]`. Final user-facing HTML очищается от `<tool_use>...</tool_use>` (и дефанг `++TOOL:...++`) через `_strip_tool_calls` ДО отправки юзеру.
- Cap раундов: `AI_TOOL_MAX_ITERATIONS` (default 3). Templates — в `utils/ai_prompts.py` (`TOOL_RESULT`, `REGEX_HEADER`, `DEBUG_HEADER`, etc).

### Память диалогов (`utils/ai_memory.py`)
- Хранится в `ai_memory.json` (version 2, atomic write+fsync, load на импорте) + in-memory кэш: `{ (user_id, chat_id): [{"role": ..., "content": ...}, ...] }`.
- `MAX_HISTORY = 40` — последних 40 сообщений на каждый диалог (FIFO-trim, чётная capacity без orphan-assistant).
- Переживает рестарт бота; TTL записей 30 суток, cap 500 топиков (FIFO-eviction без LRU).
- В Telethon-пути контекст диалога **объединяется** с thread-контекстом: `context = (thread_ctx or []) + history` (Telethon собирает соседей сам, в отличие от aiogram-пути).
- Размеры каждого сообщения в истории обрезаны до 2000 символов (для обеих сторон: `user_content`, `answer`).

---

## Утилитарные модули (utils/)

| Файл | Что делает |
|---|---|
| `storage.py` | Глобальные словари (watched_chats, user_sessions, photo_settings, auto_tr_chats, user_timezones, nya_chats, knowledge_settings) + JSON-load/save через `_atomic_write` (write→`.tmp`→`os.replace`). Хелперы для chat/thread проверок (`thread_key`), photo permissions, session path, was_processed. |
| `telethon_manager.py` | `TelethonManager` (клиенты, таски, locks, lifecycle). Все Telethon dot-handlers (dns/ip/unshort/hash/uuid/b64/timezone/tr/auto-tr/calc/save/link/love). Formatters `_format_me/chat/who_telethon`, `_topic_name_async`, `_detect_avatar_ext`, `parse_dot_targets`. `cleanup_orphan_sessions`, `auth_state_cleaner`, singleton `telethon_manager`. |
| `bot_info.py` | Динамические хелперы `bot_username()`, `bot_username_at()`, `bot_mention_html()`. Fallback на `"slimbot"` если `cfg.BOT_USERNAME = None`. Использовать на каждый запрос — не на module-scope. |
| `suggest.py` | Левенштейн по `_helpdb._PAIRS`. Кэш `_index_cache`. Возвращает ENGLISH canonical (`suggest()`) или готовую строку-подсказку (`suggest_text()`). |
| `ai.py` | OpenAI-совместимый wrapper → OpenCode Zen. `ask(prompt, context, image_bytes, image_mime) → (text, err_or_None)`. Bearer-token из env `AI_API_KEY_ZEN`. |
| `ai_memory.py` | dict-хранилище истории per-(user, chat). `get/add/clear/clear_all/size/total_size/chat_count`. |
| `calc.py` | AST-eval без `eval`. Whitelist: `+ - * / // % ** & \| ^ << >>`, константы `pi/e/tau/inf/nan`, функции `abs/round/min/max/sum/pow/sqrt/log/log2/log10/exp/ceil/floor/trunc/sin/cos/tan/asin/acos/atan/atan2/sinh/cosh/tanh/degrees/radians/gcd/factorial/hex/bin/oct`. |
| `hashing.py` | `hash_text(algo, text)` / `hash_bytes(algo, data)` / `gen_uuid(version=4)` / `gen_uuids(n)` / `b64_op(text, mode)`. ALGOS список: 11 алгоритмов включая sha3 и blake2. |
| `linkcheck.py` | `check(url)` (с TLS, тип контента, цепочка редиректов, риск-эвристики по TLD/blacklist, разница hostname start/end). `extract_url(args, reply_text)` — regex из аргументов или из replied текста. `SUSPICIOUS_TLDS` и `SHORTENERS` наборы. |
| `netinfo.py` | `dns_lookup(host, type)` (через `dns.asyncresolver`), `ip_info(target)` (через ipinfo.io, free 50k/мес), `unshorten(url, max_hops=15)` (HEAD/GET серия). |
| `translate.py` | `translate(text, target, source="auto")` через `translate.googleapis.com/translate_a/single` (без ключа). Возвращает `(translation, detected_src_lang)`. |
| `timezones.py` | `_canonicalize` (offset/IANA/RU_ALIASES → canonical), `format_with_tz` (datetime.now → str в локали юзера), `parse_tz`, `TZ_PRESETS` (русские зоны + мировые), `RU_ALIASES` (много русских имён), `is_reset_value`. `_resolve_tz` строит `tzinfo` через `zoneinfo.ZoneInfo` (Python ≥ 3.9) или fallback на `datetime.timezone`. |

---

## Структура файлов

```
slimbot/
├── bot.py                  # Точка входа. aiogram Dispatcher + Telethon старт в on_startup.
├── config.py               # TOKEN, API_ID, API_HASH, BOT_NAME, пути (sessions/, temp/),
│                           #   пути JSON (watch/sessions/photo/auto_tr/user_tz).
│                           #   СЕКРЕТЫ В ОТКРЫТОМ ВИДЕ — желательно
│                           #   вынести в .env (issue в AGENTS.md ниже).
├── requirements.txt        # aiogram >=3, telethon, aiohttp, dnspython.
├── AGENTS.md               # Этот файл.
│
├── watched_chats.json      # user_id → [[chat_id, thread_id]].
├── user_sessions.json      # user_id → {phone, has_2fa, status}.
├── photo_settings.json     # user_id → {enabled, mode, exceptions}.
├── auto_tr_chats.json      # user_id → [chat_ids...].
├── user_timezones.json     # user_id → '+3'/'Europe/Moscow' (canonical-строка).
│
├── sessions/               # Telethon .session/{-journal} файлы.
├── temp/                   # Скачанные view-once медиа (по факту использования удаляются).
│
├── utils/
│   ├── __init__.py
│   ├── storage.py          # JSON, watched/photo helpers, was_processed, session_path, thread_key.
│   ├── telethon_manager.py # TelethonManager, thread_id_of, telethon_reply_to,
│   │                       #   entity formatters (.me/.chat/.who), auto-tr handler,
│   │                       #   download_view_once, download_avatar_bytes, parse_dot_targets,
│   │                       #   cleanup_orphan_sessions, auth_state_cleaner.
│   ├── ai.py               # Fixed Willow API ask (gpt-5.6-luna, low effort).
│   ├── ai_memory.py        # JSON-персистентная история диалогов (MAX_HISTORY=40).
│   ├── calc.py             # AST eval калькулятор.
│   ├── hashing.py          # hash/uuid/b64.
│   ├── linkcheck.py        # URL check + extract_url + risk-score.
│   ├── netinfo.py          # DNS (dns.asyncresolver) + ipinfo + unshorten.
│   ├── suggest.py          # Левенштейн подсказка.
│   ├── translate.py        # Google Translate (free, ponytail endpoint).
│   ├── bot_info.py         # cfg.BOT_USERNAME динамические хелперы.
│   └── timezones.py        # TZ presets + RU_ALIASES + canonicalize.
│
├── handlers/
│   ├── __init__.py         # Пустой.
│   ├── errors.py           # logger.exception для необработанных ошибок.
│   ├── session.py          # Все callback'ы аутентификации + 2FA-text handler.
│   │
│   ├── inline/             # Единый dispatcher в __init__.py.
│   │   ├── __init__.py     # dispatch_inline, uptime_seconds, connect_button, mark_bot_started.
│   │   ├── help.py         # `помощь` keyword.
│   │   ├── status.py       # `статус` keyword (через bot.get_me() ping).
│   │   └── profile.py      # `@username` → multi-target через Telethon.
│   │
│   └── commands/
│       ├── __init__.py     # Точная последовательность include_router + 2 suggest-fallback'а.
│       ├── _base.py        # format_ping/time/id/help, timezone, thread_kwargs,
│       │                   #   reply_in_topic, answer_in_topic.
│       ├── _helpdb.py      # HELPS dict, ALIASES (alias→key), _PAIRS (key→aliases),
│       │                   #   HELP_WORDS, is_help_request, render. Подключён первым через cmdhelp.
│       ├── cmdhelp.py      # `.cmd справка` / `.cmd help` / `.cmd ?` / `.cmd хелп` интерсептор.
│       ├── extra.py        # `.me/.я`, `.chat/.чат`, `.who/.кто` (aiogram-путь; для Telethon — tlm).
│       ├── help.py         # `/help`, `.help` + клавиатуры.
│       ├── ping.py         # `.ping`.
│       ├── time.py         # `.time` (timezone-aware).
│       ├── id.py           # `.id`.
│       ├── love.py         # `.love` (ROWS+PULSE анимация).
│       ├── coin.py         # `.монетка/.coin/.монета/.орёл/.решка`.
│       ├── start.py        # `/start` + session keyboard.
│       ├── status.py       # `/status`.
│       ├── logout.py       # `/logout` + `logout_ask/yes/no` callback'и.
│       ├── watch.py        # `.watch/.unwatch/.watched` (handle_telethon + auto photo).
│       ├── netcmds.py      # `.net/.hash/.uuid/.b64`.
│       ├── tools.py        # `.tr/.calc/.save`.
│       ├── ai.py           # `.ии/.ai/.ии?` (aiogram + handle() для Telethon).
│       ├── timezone.py     # `.timezone/.таймзона/.tz`.
│       ├── delmsg.py       # `.del/.удалить` (Telethon-only).
│       ├── dm.py           # `.влс/.vls/.лс/.dm` (Telethon-only).
│       ├── tagall.py       # `.tagall/.тегвсех/.все` (Telethon-only).
│       ├── admins.py       # `.admins/.админы` (Telethon-only).
│       ├── pin.py          # `.pin/.закрепить/.закреп`, `.unpin/.открепить/...` (Telethon-only).
│       └── invitelink.py   # `.ссылка/.invitelink/.инвайт/.invite` (Telethon-only).
```

---

## Важные нюансы

1. **Секреты в `.env`.** `TOKEN`, `API_ID`, `API_HASH`, `AI_API_KEY_WILLOW` читаются через `os.getenv(...)`; живых значений в `config.py` нет (только пустые defaults — репозиторий безопасно публиковать). `.gitignore` исключает `.env`. Есть `.env.example` template в корне (копировать в `.env`, `chmod 600`). `config.py::_check_secrets()` пишет WARNING на старте если секрет не задан (не фатально). Для production задать Tier 1 (`TOKEN`/`API_ID`/`API_HASH`/`AI_API_KEY_WILLOW`) в `.env` или `systemd Environment=`. См. `config.py` (там секции и defaults).
2. **Порядок include_router в `bot.py` (commands → inline → session → errors) И commands** — не меняй ради косметики, все фильтры и приоритеты рассчитаны на текущий порядок.
3. **`was_processed` cleanup — ленивый (на access)**, не фоновый sweep. Если нужна adversarial-cleanup, иначе поток в норме.
4. **`thread_id_of` приоритеты**: если `forum_topic=True` — берёт `reply_to_top_id` (корень топика), потом fall back на `reply_to_msg_id`. Если `forum_topic=False` (обычная супергруппа) — `reply_to_top_id` если есть. Это удерживает ответ **внутри одного топика**.
5. **Telethon игнорирует ЛС с ботом**: `event.is_private and (chat.bot is True) → return`. Не добавляй новых обработчиков outgoing'а для самого бота.
6. **Inline`: single entry-point обязателен** (`@router.inline_query()` только в `__init__.py`). Не дублируй фильтр в submodule'ах.
7. **HTML-escape**: всё, что приходит из MTProto (first_name, last_name, username, about/bio, chat title) и идёт в HTML-вывод, ОБЯЗАНО через `_esc()` (`&<> → &amp;&lt;&gt;`). Это не косметика — без неё Telegram рендерит произвольный HTML.
8. **`force_sms=True` deprecated**: код приходит ТОЛЬКО как push-уведомление Telegram, не SMS. В коде этого параметра нет.
9. **Inline cache_times**:
   - authorized help: `300`, unauthorized help: `30`.
   - status: `15` для авторизованных, `10` для unauthorized (`cache_time=30/5` в profile неавторизованных ветках).
   - profile: `CACHE_TIME_SINGLE=300`, `CACHE_TIME_MULTI=180`.
10. **Авто-сохранение фото**: выключено по умолчанию. Включается автоматически при первом `.watch`, если есть Telethon-сессия. Для остальных чатов всё так же `enabled: false` в `photo_settings.json`.
11. **Inline `bot.username`** берётся через `cfg.BOT_USERNAME`, который выставляется в `bot.py::on_startup` из `bot.get_me().username`. **Не хардкоди `@slimbot`** — все handler'ы вызывают `bot_username_at()` / `bot_mention_html()` через `utils.bot_info` (они runtime-safe).
12. **Sessions — orphaned cleanup** работает при старте: пока `user_sessions.json` не отредактирован, `.session` файлы без соответствующей записи УДАЛЯЮТСЯ.
13. **`handlers/inline/profile.py` целевой лимит**: `INLINE_USER_LIMIT = 5` (`.who` в Telethon — `parse_dot_targets(limit=5)`).
14. **`was_processed` ключ = `(chat_id, msg_id, thread_id)`**: в одном чате тот же `msg_id` в разных топиках считаются разными сообщениями.
15. **Рассылка view-once**: только Telethon-путь `_handle_incoming` — сообщение в личку ОТ БОТА через `telethon_manager._bot.send_photo/send_video`. Файл из `temp/` удаляется после успешной/неуспешной отправки.
16. **`connect_button`** строит `InlineQueryResultsButton(start_parameter="start")` для unauthorized-веток inline'а. Когда юзер жмёт, Telegram открывает личку с ботом и автоматически отправляет `/start`. Используется в profile неавторизованных и status неавторизованных.
17. **Кэш удалений удалён**: `message_cache` / `CACHE_SIZE` / `DATA_FILE` удалены из `storage.py` и `config.py` вместе с удалением Bot API-пути; восстановление удалённых сообщений не поддерживается.
18. **Калькулятор (.calc) безопасный** — `ast.parse(..., mode='eval')` + whitelisted BinOp/UnaryOp/Name/Call/Tuple. Никаких subscript, comprehension, attribute.
19. **Inline persists user-state**: `_BOT_START_TS` (uptime) живёт в `handlers/inline/__init__.py`. Inline-cache'и работают стандартно через Telegram CDN, поэтому `cache_time` влияет только на freshness для клиента, не на серверную нагрузку.
20. **multi-target .who в Telethon**: при screenshot edit-summary не использует typewriter — просто `event.edit()`. Быстрый фолбэк для случаев, когда все targets зафейлили.
21. **`.вгф` video-path требует `ffmpeg` в PATH** (внешний бинарник, **не** Python-зависимость — в `requirements.txt` его нет и не должно быть). Команда сама проверяет через `shutil.which("ffmpeg")` (lazily cached) и при отсутствии выдаёт пользователю `"[?] ffmpeg не найден в PATH"`. ffmpeg нужен только для видео-ветки (анимированная GIF, palettegen+paletteuse single-pass, bounds 8с×720×18fps, hard timeout 60с через `asyncio.wait_for` + `proc.kill()`). Pillow остаётся обязательным только для photo-ветки. Минимальные версии ffmpeg: ≥4.4 (для `palettegen=stats_mode=full`, старые сборки деградируют на `dither=sierra2_4a` — fallback на дефолтный dither). В `apt` ставится как `apt-get install -y ffmpeg`, в Docker `mdaffin/docker-ffmpeg` / `linuxserver/ffmpeg` / `jrottenberg/ffmpeg`.
