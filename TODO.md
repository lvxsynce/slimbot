# TODO — Slim Bot: исправление найденных проблем

Правила: каждый пункт закрывается тестом. После каждого шага — прогон `pytest`; если
красное — чиним, если зелёное — идём дальше. Регрессии не допускаются.

Базовая линия до начала работ: **279 passed**.

---

## Phase 0 — Базовая линия
- [x] P0.0 Прогнать тесты, зафиксировать baseline (279 passed)

---

## Phase 1 — P0: ломающие баги

- [x] P1.1 `ignore_unauthorized` глушит все dot-команды для юзеров без сессии
  - `handlers/commands/__init__.py:40` — хендлер на родительском роутере возвращает
    `None`, aiogram не доходит до `include_router(...)`.
  - Фикс: `raise SkipHandler()`; оба fallback-хендлера перенести в суб-роутер,
    подключаемый ПОСЛЕДНИМ.
  - Тест: dispatcher-уровневый, юзер без сессии → `.ping` отвечает; юзер с сессией → отвечает.

- [x] P1.2 `was_processed` без `user_id` → кросс-пользовательское подавление view-once
  - `utils/storage.py:60` + 3 call sites (`telethon_manager.py:940`, `:1735`, `love.py:166`).
  - Фикс: `user_id` в ключ.
  - Тест: два юзера, один чат+сообщение → оба получают `False`.

- [x] P1.3 `.time` возвращает вложенный `<blockquote>` (оба пути)
  - `utils/timezones.py:38` + `utils/texts.py:255`.
  - Фикс: `format_with_tz` возвращает голый текст; карточку собирает `render_time`;
    `Texts.Time.TIME` больше не оборачивает. Попутно удалены мёртвые
    `format_time` / `format_id` (P4.2) и их неиспользуемые импорты.
  - Тест: `tests/test_cards_nesting.py` — ровно один `<blockquote>`, структурная
    проверка вложенности на `render_time` / `render_id` / `command_card`.

- [x] P1.4 `.del` кидает необработанное исключение каждый вызов
  - `handlers/commands/delmsg.py` — `event.id` удаляется дважды.
  - Фист: не удалять повторно, глотать `MessageIdInvalidError`.
  - Тест: `delete_messages` включает `event.id` → `event.delete()` НЕ вызывается.

---

## Phase 2 — P1: безопасность и злоупотребления

- [x] P2.1 Нет allowlist на подключение сессии
  - `handlers/session.py:165` — любой может подключить аккаунт.
  - Фикс: ✅ `SESSION_ALLOWLIST` в config (пустой = открытый режим + WARNING в лог),
    гейт в `connect_cb` И в `_start_auth` (повторная проверка), парсинг
    запятых/точек-с-запятой.
  - Тест: ✅ `tests/test_session_auth.py` — 15 тестов, включая парсинг env.

- [x] P2.2 `.tr` без rate-limit и без авторизации → сжигание LLM-ключа
  - Фикс: ✅ общий LLM-бюджет `"ai"` с `.ии` (не обойти через соседнюю команду).
  - Тест: ✅ лимит срабатывает; общий bucket; при отказе `_do_tr` не вызывается.

- [x] P2.3 `.net/.hash/.uuid/.b64/.opencode/.quote/.love` без rate-limit
  - Фикс: ✅ единый `allow()` в aiogram-пути (`netcmds`, `opencode`) и
    расширенный список в Telethon-пути (добавлены `.tr`, `.uuid`, `.b64`,
    `.quote`, `.love/.govno`, `.опенкод`; `.love` ест FloodWait-бюджет
    ОБЩЕГО токена бота, то есть бьёт по всем пользователям).
  - Тест: ✅ 18 тестов — блокировка/пропуск в каждой точке + статический
    инвариант на список в `_handle_outgoing`.

- [x] P2.4 2FA-пароль остаётся в чате
  - `handlers/session.py:420` — нет `message.delete()`.
  - Тест: ✅ после sign_in (успех/провал) и при rate-limit сообщение удалено.

- [x] P2.5 `_finish` пишет `status:"active"` при неудачном старте клиента
  - `handlers/session.py:465-475`.
  - Фикс: ✅ `status:"active"` только после успешного `start_client`; обе точки
    вызова сообщают об отказе; попутно `invalidate_premium_cache(uid)`
    (иначе 5 минут unicode-эмодзи после подключения).
  - Тест: ✅ `start_client` → False ⇒ нет записи, `session_exists` False, auth_state снят.

- [x] P2.6 `logout()` всегда возвращает `True` → враньё в UI
  - Фикс: ✅ возвращает `revoked`; клиент и `.session` чистятся в любом случае.
  - Тест: ✅ `log_out` падает ⇒ `False`, клиент всё равно снят.

- [x] P2.7 `.tr auto` глушит `.ня` (первая ветка делает `return`)
  - Фикс: ✅ обе ветки создают задачи; `return` убран; имена задач для диагностики.
  - Тест: ✅ обе фичи включены ⇒ обе задачи; поодиночке ⇒ одна; ни одной ⇒ ноль.

- [x] P2.8 `_auto_tr_messages` снимает guard у живой задачи, нет TTL
  - Фикс: ✅ не снимать в раннем return; `_auto_tr_seen` + `sweep_auto_tr_messages()`
    (вызывается из health-check); чистка в `stop_client`/`logout`.
  - Тест: ✅ повторный dispatch не создаёт вторую задачу; ключ снят после
    завершения; свип вычищает протухшие; вызов из `check_clients_health`.

---

## Phase 3 — P1/P2: надёжность и event loop

- [x] P3.1 `probe_duration` (sync subprocess) блокирует loop
  - Фикс: ✅ добавлен `probe_duration_async` (`asyncio.to_thread`); async-пути
    зовут только его, sync-версия помечена «зови только через async-обёртку».
  - Тест: ✅ loop не встаёт во время «ffprobe»; статический инвариант — в
    async-функциях `gif_converter` нет `subprocess.run`.

- [x] P3.2 CPU-bound Pillow/rlottie в async (`quote.py:138,156,159,273`)
  - Фикс: `run_in_executor`.
  - Тест: рендер `.q` не блокирует loop (вспомогательный таймер срабатывает).

- [x] P3.3 Утечка ffmpeg-процессов при `CancelledError`
  - Фикс: ✅ все три хелпера ловят `(TimeoutError, CancelledError)` и убивают процесс.
  - Тест: ✅ эмуляция отмены задачи ⇒ `kill()` вызван (×3); структурная проверка,
    что `kill()` внутри именно CancelledError-обработчика.

- [x] P3.4 Синхронный sqlite блокирует loop (замерено: `count_messages` 52–114 мс,
  `DELETE` 200k строк — 10.3 с, `VACUUM` 525 МБ — 1.9 с, всё под глобальным
  `RLock`, который не защищает от loop)
  - Фикс: ✅ добавлен `knowledge_db.run_db(fn, ...)` = `asyncio.to_thread`;
    все 29 вызовов в `knowledge_collector` и 3 в `ai.py` переведены;
    `logout` → `await asyncio.to_thread(clear_owner, ...)`.
    `VACUUM` убран из `clear_owner` (глобальный эксклюзивный лок 1.9 с),
    осталась ручная `knowledge_db.vacuum()` для обслуживания.
  - Тест: ✅ 9 тестов — loop крутится во время чтения и clear_owner;
    AST-проверка отсутствия VACUUM; статический инвариант «async-код только
    через run_db».

- [x] P3.5 `ai_memory._save()` — синхронный полный dump + fsync на каждом сообщении
  - Фикс: ✅ coalescing-архитектура: мутатор только взводит `_dirty` и
    планирует фоновую задачу (`asyncio.to_thread`); `await flush()` даёт точку
    гарантии долговечности (вызывается в обоих путях `.ии` и в `.ии сброс`).
  - Тест: ✅ 14 тестов — мутатор не пишет файл (замер времени < 0.1 с при
    0.3 с записи), запись идёт в другом потоке, coalescing (100 мутаторов →
    ≤5 записей), долговечность clear/clear_all, round-trip через reload.

- [x] P3.6 Голые `client.get_entity` без таймаута
  - `handlers/commands/watch.py:196`, `admins.py:30`, `utils/premium.py:117`.
  - Фикс: ✅ `asyncio.wait_for` в `watch._resolve_chat_link` (цикл по чатам!),
    `watch` (get_me), `admins` (GetParticipants + каждый get_entity, с
    `continue` при фейле одного участника), `knowledge._dialogs`,
    `premium.is_user_premium` (свой `_PREMIUM_RPC_TIMEOUT` — путь почти всех
    команд через `render_for_user`).
  - Тест: ✅ 19 тестов — статический инвариант «нет голого RPC без wait_for»
    по 3 модулям + динамика на реальном длинном выводе.

- [x] P3.7 Нет троттлинга 4096 символов
  - `handlers/commands/_base.py::command_card` + длинные выводы (`.b64`, `.net`,
    `.watched`, `.ии`).
  - Фикс: ✅ `_truncate()` + вызов в `command_card`; закрывает незакрытые
    `<code>/<b>/<i>/<s>/<u>/<a>/<blockquote>`; маркер «…обрезано».
  - Тест: ✅ 4096-лимит, баланс тегов, реальные случаи (.net, .b64 3200 симв.).

- [x] P3.8 `load_photo_settings` без валидации → AttributeError теряет view-once
  - Фикс: ✅ полная нормализация (`enabled`/`mode`/`exceptions` + whitelist
    режимов) прямо в `load_photo_settings`; не-dict значения отбрасываются.
  - Тест: ✅ список вместо dict / неизвестный mode / отсутствующие поля —
    `is_photo_allowed` не падает.

- [x] P3.9 `load_*` в storage без isinstance-guard (user_sessions, watched, tz, templates)
  - Фикс: ✅ isinstance-гарды в `load_user_tz` / `load_watched` (остальные
    уже имели); `save_watched` под mock в тестах.
  - Тест: ✅ 9 загрузчиков на битых структурах (list вместо dict и т.п.).

- [x] P3.10 `premium_render` — мёртвый параметр в 3 форматтерах
  - Фикс: ✅ параметр удалён из трёх форматтеров + вычислители `sender_premium`
    (4 вызова) и неиспользуемый импорт `is_entity_premium`.

- [x] P3.11 `watch.py::_auto_enable_photos` KeyError на битых settings
  - `handlers/commands/watch.py:32-44` (прямой доступ по ключам).
  - Фикс: ✅ доступ через `.get()` + нормализация; заодно `_disable_photos_for_chat`
    больше не затирает все чужие исключения при переходе `all → all_except`.
  - Тест: ✅ оба сценария + регрессия на потерю исключений.

- [x] P3.12 Утечка `_AUTH_LOCKS` / `rate_limit._BUCKETS` (нет prune)
  - Тест: prune работает.

---

## Phase 4 — Мёртвый код

- [x] P4.1 Удалить мёртвые константы `Texts.*` (~28)
- [x] P4.2 Удалены `format_ping`, `format_time`, `format_id`; в работе —
  `command_response`, `normalize_command_card`, `reply_in_topic`,
  `answer_in_topic`, `ai_memory.add/total_size/set_summary`,
  `hashing.gen_uuid`, `premium.is_aiogram_message_premium`, `thread_key`
- [x] P4.3 Сделано по `ai_memory`/`hashing`/`premium`/`storage.thread_key`
  (см. P4.2); остаток — `ai_prompts` и `media_kind` (см. ниже)
- [x] P4.4 Удалить мёртвые `_check`/`*_CMDS` в Telethon-only модулях
  - Решение: ✅ вместо удаления убран ИСТОЧНИК дрейфа — новый листовой модуль
    `utils/cmds.py` (единый реестр алиасов + бюджеты rate-limit). 8 модулей
    команд и `_handle_outgoing` импортируют из него; мёртвые `_check()` удалены,
    31 хардкод-кортеж заменён константами реестра.
  - Побочно найдено и исправлено: `.пер` отсутствовал в `_helpdb._PAIRS`
    (⇒ `.пер справка` не работала, `.перр` не подсказывался); `.ии?` и
    `+шаб`/`-шаб` были в реестре без справки; `admins.py` импортировал
    константу, не используя её.
  - Тест: ✅ 34 теста `test_command_registry.py` — реестр листовой, без
    дублей, бюджеты не пересекаются, реестр ⟷ `_helpdb` согласованы в обе стороны.
- [x] P4.5 Убран `_BYPASS_HEAD = ()` + мёртвая head-проверка в `_bypass`
- [x] P4.6 Убраны неиспользуемые импорты в горячих модулях
  (`telethon_manager`, `watch`, `admins`, `admins` ×2, `time`, `id`, `delmsg`).
  Тест-инвариант ловит новые.
- [x] P4.7 Тест-инвариант: ни один `Texts.*` константа не мёртвая (авто-скан)

---

## Phase 5 — Дублирование

- [x] P5.9 Схлопнуть 5 копий `_esc` в `utils/escape.esc`
- [x] P5.9 Схлопнуть 3 копии `thread_id_of` в одну
- [x] P5.9 Убрать дубликат `_extract_telethon_usernames`
- [x] P5.9 Убрать 20× `from html import escape as _h` в пользу `utils.escape.esc`
- [x] P5.9 Единый `.ping` (оба пути дают разный вывод)
- [x] P5.9 Единый `.b64` (aiogram-путь не поддерживает `url`)
- [x] P5.9 Единый `.watched` (ссылки vs `<code>`)
- [x] P5.9 Единый `.timezone` / `.coin`
- [x] P5.9 Хинг `.ии` в одном месте (3 копии)

---

## Phase 6 — Документация

- [x] P6.5 Переписать AGENTS.md по факту состояния кода
  (убрать `_handle_auto_tr_incoming`, `translate.py`, `AI_API_KEY_ZEN`,
  старые probabilities, неверный `include_router` порядок, мёртвый `thread_key`)
- [x] P6.5 Дописать `.govno`, `.ии база`, `.опенкод` в таблицы команд
- [x] P6.5 Дополнить секцию структуры файлов (11 utils-модулей, 6 commands, tests/)
- [x] P6.5 Починить ложные help-строки в `_helpdb.py`
  (`.вгф` видео/стикеры, `.watch` удаления, `.tr auto` входящие,
  `.quote` 1200px, `.hash` 8→10 алгоритмов, `.uuid` опечатка)
- [x] P6.5 Тест-дрейф: `_helpdb` ↔ реальные команды ↔ AGENTS.md

---

## Phase 7 — Регресс-защита

- [x] P7.1 Dispatcher-тест (иначе P1.1 вернётся) — ✅ `tests/test_dispatcher_routing.py`:
  у родительского роутера ноль своих хендлеров, fallback последний, SkipHandler
  на реальной команды, подсказка на опечатке — обе ветки
- [x] P7.2 Тест на совпадение алиасов с диспатчем — ✅ `tests/test_command_registry.py`
- [x] P7.3 Тест на отсутствие вложенных `<blockquote>` — ✅ `tests/test_cards_nesting.py`

---

## Phase 8 — Баги, найденные адверсarial-проверкой моих же фиксов

- [x] P8.1 `ai_memory.flush()` теряет записи (3 связанных бага)
  - `_flush_task` никогда не сбрасывается в `None` ⇒ ветка восстановления недостижима;
    `_dirty` навсегда `True` без записи.
  - `_schedule_save` при живой задаче НЕ планирует новую ⇒ дельта осиротевает.
  - `flush()` — no-op при `_flush_task is None and _dirty is True`.
- [x] P8.2 `_write_now` сбрасывает `_dirty` ДО записи ⇒ падение (ENOSPC) теряет дельту
- [x] P8.3 Дедлок: `await _write_now_async()` внутри `with _lock` (RLock не кросс-потоковый)
- [x] P8.4 `_truncate` меряет code points, а Telegram — UTF-16 ⇒ эмодзи-карточки всё ещё >4096
- [x] P8.5 `.time` печатает заголовок «Текущее время» ДВАЖДЫ
- [x] P8.6 `_finish`: при неудаче `start_client` `.session` не пишется в реестр ⇒
  `cleanup_orphan_sessions` удалит его на следующем старте
- [x] P8.7 `.love`/`.govno` под лимитом только в Telethon-пути, в aiogram — нет
- [x] P8.8 Один общий bucket на все дорогие команды (`.love` блокирует `.net`)
- [x] P8.9 `MAX_ACTIVE_SESSIONS` объявлен, но не используется
- [x] P8.10 `SESSION_ALLOWLIST` не отражён в UI (`/start`, `/status`)
- [x] P8.11 `Texts.render` — второй конструктор карточек, без `_truncate`
- [x] P8.12 `command_card`: дублирующийся мёртвый блок
- [x] P8.13 Порядок импортов (E402) в delmsg/admins
- [x] P8.14 Flaky-тест `test_audit_wave3` (sleep вместо ожидания события)

### Итог Phase 8
Все 13 подтверждённых проблем исправлены; 552 теста стабильно зелёные
(проверено на 3 прогонах подряд).

**Нашёл и исправил в том числе:**
- Реальную гонку двух записей `ai_memory` на одном `.tmp`-файле
  (FileNotFoundError + полузаписанные данные) — ввёл `_write_lock`.
- Регекс, который я сам внёс и который «схлопнул» 304 строки в
  `inline/profile.py` и 543 в `template.py` — оба восстановлены из git;
  дальнейшие правки сделаны точечно. **Вывод: жадные `re.sub` по .py
  опасны; использовать точечные замены + проверку `git diff --stat`.**

---

## Phase 9 — Доделка

- [x] P3.2 CPU-bound Pillow/rlottie → `_to_thread` (5 мест в `quote.py`)
- [x] P3.12 Утечка `_AUTH_LOCKS`: `_auth_lock()` + `_prune_auth_state` по всем трём словарям
- [x] P4.1 Удалено **38** мёртвых `Texts.*` (2 волны: 31 + 7, порождённые правками)
- [x] P4.7 Сканер + `tests/test_texts_no_dead.py` — AST, не регулярка
- [x] P5.5 Единый `.ping` (`shared_cmd.ping_body` + `_ping_pairs`)
- [x] P5.6 Единый `.b64` (`parse_b64_args` / `b64_body`) — `url` работает в обоих путях
- [x] P5.7 Единый `.watched` (`watched_body`) + обрезка до 40 строк
- [x] P5.8 Единый `.timezone` (`timezone_body`) и `.coin` (`coin.flip`)
- [x] P5.9 Подсказка `.ии` — одна функция, различия через параметры
- [x] P6.1–P6.3 AGENTS.md переписан по факту + таблицы Telethon-only и структуры
- [x] P6.4 `_helpdb`: 6 ложных утверждений исправлены (`.watch`, `.hash`,
  `.uuid`, `.quote` 1200→1400px, `.вгф`, `.tr auto` исходящие)
- [x] P6.5 `tests/test_docs_drift.py` (26 тестов) + `test_helpdb_accuracy.py` (81)

### Итог

**279 → 719 тестов, все зелёные.** Закрыто 57 пунктов TODO.

Новые модули: `utils/cmds.py` (реестр), `utils/shared_cmd.py` (общая логика),
`utils/tlm_common.py`, `utils/tlm_rpc.py`, `utils/rate_limit_gate.py`.

Новые защитные тесты: `test_dispatcher_routing`, `test_processed_guard`,
`test_cards_nesting`, `test_delmsg`, `test_rate_limit`, `test_rate_limit_coverage`,
`test_session_auth`, `test_session_limits_and_ui`, `test_tlm_logout_and_hooks`,
`test_knowledge_db_loop`, `test_ai_memory_persistence`,
`test_ai_memory_flush_guarantee`, `test_ffmpeg_loop_safety`,
`test_rpc_timeouts_and_truncation`, `test_storage_robustness`,
`test_dead_code_removed`, `test_command_registry`, `test_texts_no_dead`,
`test_auth_state_growth`, `test_shared_command_logic`, `test_quote_loop_safety`,
`test_truncate_utf16_and_time_card`, `test_docs_drift`, `test_helpdb_accuracy`.

### Открыто (осознанно)

- `utils/tlm_rpc.py` создан, но ещё не переведён на него весь Telethon-слой:
  обёртки `resolve`/`get_me`/`send` дублируют `asyncio.wait_for` в 4 модулях.
  Не опасно, но и не доведено до конца — кандидат на следующий заход.
- `Texts.Ping.PING` остался неиспользуемым после перевода `.ping`
  на общий форматтер; чистится следующим прогоном сканера.
