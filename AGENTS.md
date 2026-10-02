# AGENTS.md — архитектура Slim Bot

> **Документ проверяется тестами.** `tests/test_docs_drift.py` падает, если
> расходятся: список файлов, порядок `include_router`, алиасы команд в
> `utils/cmds.py` и `handlers/commands/_helpdb.py`, упоминаемые модули.
> Правя код — правь и этот файл.

## Где живут секреты

По модели **Heroku config vars**: то, что меняется между деплоями и
содержит секреты, лежит в окружении, а не в репозитории.

| Где | Что | Права |
|---|---|---|
| `/etc/slimbot/secrets.env` | боевые секреты и per-deploy настройки | `600`, `root:root` |
| `/etc/systemd/system/slimbot.service.d/override.conf` | `EnvironmentFile=/etc/slimbot/secrets.env` | — |
| `.env` в корне | **только локальная разработка**, пустые значения | `600`, в `.gitignore` |
| `.env.example` | шаблон с пустыми значениями | в репозитории |
| `config.py` | только чтение `os.getenv` и дефолты | в репозитории |

`tests/conftest.py` задаёт фиктивные значения **до** импорта `config`,
поэтому тесты не требуют ни боевых ключей, ни `.env`.

Правила:

* `_int_env`/`_float_env` на пустое или мусорное значение берут дефолт, а
  не падают: переменную можно не задать или снять (`heroku config:unset`
  тоже оставляет её пустой);
* недостающие секреты — WARNING на старте (`_check_secrets`), не исключение;
* `.env` **не** источник продакшн-конфигурации. На сервере он пустой.

Проверка перед коммитом:

```
git grep -nI "sk-\|Bz9ZrOY\|1f9e5104" -- . ':!*.example'
```

---

## Что это

Telegram-бот, в котором пользователь подключает **свой аккаунт** через
Telethon (MTProto), и тогда dot-команды работают **в любых чатах** от его
имени.

| Слой | Технология | Где работает |
|---|---|---|
| `bot.py`, `handlers/commands/`, `handlers/session.py`, `handlers/inline/` | aiogram (Bot API) | **только личка с ботом**: `/start`, auth-кнопки, inline |
| `utils/telethon_manager.py` | Telethon (MTProto) | **все остальные чаты** — sole executor |

LLM-бэкенд (`.ии`, `.tr ai`) — ddt-прокси, OpenAI-совместимый
`/v1/chat/completions`. Два отличия от обычного OpenAI: заголовок
`X-DDT-Group` — **приоритет** группы, а не фильтр (нет ключа под модель →
запрос уйдёт в другую группу), и `model` в теле ответа. Поэтому в футере
ответа `.ии` подписывается фактическая модель, а не заданная в конфиге
(`AI_SHOW_MODEL`). Клиент: `utils/ai.py`, модели и фолбэк — `config.AI_MODEL*`.

Telegram Business API **не используется**. Из проекта удалены:
`handlers/business.py`, `handlers/messages.py`, `handlers/deletions.py`.
Мониторинг удалённых сообщений не поддерживается.

---

## Масштабирование: сколько сессий держит один процесс

Механика рассчитана на многие аккаунты, но у процесса есть явные потолки.

**Что работает:** состояние изолировано по `user_id` почти везде
(`watched_chats`, `photo_settings`, `auto_tr_chats`, `nya_chats`,
`user_timezones`, `knowledge_settings`, `templates`, `rate_limit`,
`premium`-кэш, `ai_memory`, `user_sessions`). Менеджер держит
`dict[str, TelegramClient]` без искусственного потолка.

**Потолки (замеры на реальной базе 525 МБ / 200 647 сообщений):**

| Потолок | Симптом | Что сделано |
|---|---|---|
| 1 процесс | `TelegramConflictError` — второй long-polling невозможен | масштабировать репликами нельзя |
| event loop | SQLite на loop: 52–114 мс на `COUNT`, 10.3 с на `DELETE` 200k | `knowledge_db.run_db` = `asyncio.to_thread` |
| event loop | `ai_memory` переписывал 41.6 МБ + `fsync` = **1.57 с** на каждый `.ии` любого юзера | coalescing + `flush()` |
| event loop | `VACUUM` 525 МБ = 1.9 с **глобальным локом** | убран из `clear_owner`; осталась ручная `vacuum()` |
| event loop | ffprobe до 20 с; ffmpeg/Pillow/rlottie на loop | `probe_duration_async`, `_to_thread`, `CancelledError → kill()` |
| память | `.q`: 188 КБ → 7.4 МБ в RAM (до 40 RGBA-кадров) | ограничено `max_frames`, offloaded в поток |
| старт | 200 сессий = 200 одновременных `connect()` = flood по IP/API_ID | `SESSION_START_BATCH=5`, пауза между волнами |
| размер | `knowledge.sqlite3` — один файл на всех, лимит per-owner | 200k × N юзеров |

**Реалистичный диапазон: 20–50 сессий.** Дальше нужен `aiosqlite`,
инкрементальная запись памяти и разделение БД по владельцам.

---

## Phase 1 — Telethon (sole executor вне лички с ботом)

Подключение: `handlers/session.py` (auth-flow через callback'и и клавиатуры).
Менеджер: `utils/telethon_manager.py`.

### Хуки

| Хук | Событие | Назначение |
|---|---|---|
| `_handle_outgoing` | `NewMessage(outgoing=True)` | диспатч dot-команд |
| `_handle_incoming` | `NewMessage()` | view-once; индекс «базы знаний» |
| `_keep_alive` | — | reconnect с экспоненциальным backoff |
| `_health_check_loop` | — | liveness + свип зависших ключей |

### Защита от двойной обработки

`utils/storage.was_processed(chat_id, msg_id, thread_id=None, user_id=None)`.
**`user_id` обязателен**: без него два юзера, подключившие один чат через
`.watch`, делили ключ — и второй **молча терял одноразовое фото**, без лога
и ошибки. TTL 5 с, ленивый cleanup, thread-aware. Три вызова:
`telethon_manager._handle_outgoing`, `._handle_incoming`, `love._do_anim`.

### Авто-перевод (`.tr auto`)

Переводит **собственные исходящие** сообщения юзера. Guard
`_auto_tr_messages` держится, пока задача жива, и снимается в её `finally`;
`sweep_auto_tr_messages()` (из health-check) вычищает зависшие ключи —
задача может быть отменена до первого шага и не отработать `finally`.

**`.tr auto` и `.ня` не глушат друг друга** — обе задачи создаются.

### `.ня` (catgirl-rewrite)

Переписывает исходящие сообщения в чате через `utils/catgirl.to_catgirl`.
Фича Telethon-only: aiogram не может читать чужие исходящие. Probabilities —
`NYA_*` в `config.py`.

---

## Phase 2 — aiogram (личка с ботом)

### Порядок роутеров (`handlers/commands/__init__.py`)

```
cmdhelp → extra → help → id → love → logout → netcmds → ping → start
       → status → time → timezone → tools → watch → coin → knowledge
       → ai → opencode → modules → commands-fallback
```

`cmdhelp` первым — перехватывает `.команда справка` для всех команд.
`modules` перед fallback: иначе fallback отвечал бы подсказкой вместо
команды модуля. `commands-fallback` **последним** — и это не stylistic:
см. ниже.

> **Почему fallback вынесен в отдельный суб-роутер.**
> В aiogram 3.x `Router._propagate_event` сначала проверяет СВОИ хендлеры и,
> если хоть один вернул не-`UNHANDLED`, в `sub_routers` не заходит. Старый
> `ignore_unauthorized` жил на самом `router` и возвращал `None` (подсказки
> не нашлось) — этого достаточно, чтобы **полностью заглушить все dot-команды
> для юзеров без Telethon-сессии**. Теперь: `_fallback_router` подключён
> последним, оба его хендлера поднимают `SkipHandler`.
> Защищено `tests/test_dispatcher_routing.py`.

### Авторизация сессии

Поток целиком через callback'и и клавиатуры в личке с ботом:

```
/start → [+] Включить (c1) → цифры номера (p:*, pb, pgo)
       → push с кодом        (d:*, ddel, dgo)
       → 2FA паролем текстом
       → _finish()
```

Обфускация от паттернов Telegram: «код» → «цифры», «сессия» →
«дополнительные возможности»; кнопки идут с задержкой 0.3–2.3 с
(`_rand_delay`).

Антифлуд: `AUTH_MAX_ATTEMPTS=5` попыток за `AUTH_ATTEMPT_WINDOW=600` с,
плюс `AUTH_COOLDOWN=60` с между попытками. Словари `_AUTH_LOCKS` /
`_AUTH_ATTEMPTS` / `_AUTH_SEEN` чистятся вместе (`_prune_auth_state`):
`defaultdict` создавал по `Lock` на каждого юзера, кто хоть раз коснулся
аутентификации, и записи жили до конца процесса.

**Гейт доступа.** `SESSION_ALLOWLIST` в `config.py` (пусто = открытый
режим, но с WARNING в лог). Проверяется в `connect_cb` **и** повторно в
`_start_auth`; `/start` прячет кнопку «Включить», `/status` — подсказку.

**2FA-пароль удаляется из чата** сразу после `sign_in` (`_erase_password`).

**`_finish` и статус.** Клиент поднимается **до** записи в `user_sessions`.
При неудаче пишется `status: "pending"`, а не `None`: `sign_in` уже создал
валидный `.session`, и без записи в реестре `cleanup_orphan_sessions()`
удалил бы его на следующем старте — юзеру пришлось бы проходить аутентификацию
заново из-за сетевого сбоя. При этом `session_exists()` == `False`, то есть
UI честен.

### Лимиты и абьюз

`utils/rate_limit.py` — бакеты `(uid, операция)`, свип по возрасту, жёсткий
потолок. `utils/rate_limit_gate.py` — **единственная точка входа**: оба пути
звают её, поэтому набор ограниченных команд не может разойтись.

| Бюджет | Команды | Смысл |
|---|---|---|
| `ai` | `.ии`, `.tr` | каждый вызов платный; общий на оба, иначе обойти через соседнюю |
| `network` | `.net` | ~25 исходящих запросов на вызов |
| `heavy` | `.quote`, `.вгф`, `.опенкод` | ffmpeg / десятки секунд CPU |
| `anim` | `.love`, `.govno` | 21 `editMessageText`, едят FloodWait-бюджет **общего** токена бота |
| `local` | `.hash`, `.uuid`, `.b64`, `.calc` | `.hash` по reply скачивает файл |

Бюджеты **раздельные**: общий «network» позволял пяти дешёвым `.love`
заблокировать `.net` на минуту.

---

## Phase 3 — общие модули

| Модуль | Роль |
|---|---|
| `utils/cmds.py` | **реестр алиасов и бюджетов.** Единственный источник; `telethon_manager` и все модули команд импортируют отсюда. Листовой (без импортов проекта) — иначе цикл |
| `utils/inline_kb.py` | клавиатуры inline: `connect_button` (deep-link на `/start`) и `switch_inline_markup` (`switch_inline_query_chosen_chat`) — листовой, тянет только `aiogram.types` |
| `utils/modules.py` | реестр модулей: загрузка `.py`, изоляция по `uid`, конфликты с системными командами, отключение системных команд. Подробности — `modules_dev.md` |
| `utils/module_api.py` | `@command` / `@inline` / `Context`, которые импортирует модуль. Реэкспортируется в `slimbot_api.py` |
| `utils/module_state.py` | `ctx.state` — per-user JSON состояние модуля (`get/set/update/delete/all/clear`) |
| `utils/shared_cmd.py` | логика команд, **общая для обоих путей** |
| `utils/tlm_common.py` | `thread_id_of`, `telethon_reply_to`, `chat_link`, `usernames_of` |
| `utils/tlm_rpc.py` | обёртки RPC с таймаутом |
| `utils/storage.py` | все JSON-хранилища + `was_processed` |
| `utils/texts.py` | шаблоны текстов, premium-aware рендер |
| `utils/escape.py` | `esc()` + санация LLM-HTML |
| `utils/rate_limit.py`, `utils/rate_limit_gate.py` | лимиты |
| `utils/knowledge_db.py`, `utils/knowledge_collector.py` | SQLite FTS5 индекс переписки |
| `utils/quote_image.py`, `utils/gif_converter.py` | рендер цитат, ffmpeg/rlottie |
| `utils/media_kind.py` | классификация медиа (11 типов) |

### Почему логика команд одна

Каждая команда была реализована дважды, и копии разошлись: `.b64` в
aiogram-пути не знал про `url`, `.watched` рисовал ссылки только в одном
пути, `.ping` давал 9 строк против одной, `.timezone` держал вторую копию с
захардкоженными строками, подсказка `.ии` существовала в трёх экземплярах и
две уже отличались на строку. Теперь одна реализация, а различия путей
выражены параметрами (`has_session`, `url`, `ctx_default`).

### Карточки

- Ровно **один** `<blockquote>`; вложенность Telegram отклоняет.
- Заголовок задаёт `command_card(title, body)`, а не шаблон в `Texts`.
- `_truncate` режет по **UTF-16 code units** — именно их считает Telegram,
  а `len()` в Python даёт code points (3000 emoji = 6000 UTF-16).

---

## Phase 4 — «База знаний» (`.ии база`)

SQLite FTS5 в одном файле на всех владельцев; все запросы `WHERE owner_id=?`.

**Все async-вызовы идут через `knowledge_db.run_db(fn, ...)`** =
`asyncio.to_thread`. Прямой вызов синхронной функции бьёт по event loop:
замерено 52–114 мс на `COUNT`, 10.3 с на `DELETE` 200k строк.
`VACUUM` (1.9 с, глобальный лок) убран из `clear_owner` — осталась ручная
`knowledge_db.vacuum()` для обслуживания на простое.

Сбор: последовательный обход диалогов с чекпоинтами, дельта-проход,
`index_live` на каждое новое сообщение. Retention —
`KNOWLEDGE_MAX_MESSAGES_PER_OWNER` (per-owner, не глобально).

---

## Phase 5 — inline

Единая точка входа `handlers/inline/dispatch_inline`. Подмодули экспортируют
чистые функции, а не роутеры: несколько `@router.inline_query()` дали бы
двойной `inline.answer()` → `BadRequest`.

| Запрос | Модуль | id результата |
|---|---|---|
| `@bot`, `@bot помощь` (`help`/`справка`/`h`/`?`) | `inline/help.py` | `help_main` / `help_unauthorized` |
| `@bot статус` (`status`) | `inline/status.py` | `status_main` / `status_locked` |
| `@bot @u1 @u2 …` (до `DOT_TARGET_LIMIT`) | `inline/profile.py` | `profile_<entity_id>` / `profile_hint` / `profile_need_session` / `profile_err_<target>` |
| что угодно другое | `inline/help.py::handle_hint` | `help_unknown_query` |

Все user-controlled поля экранируются (`utils/escape.esc`).

**Подсказки.** В Bot API нельзя задать placeholder в поле ввода —
единственный механизм это inline-кнопка
`switch_inline_query_chosen_chat`. Она дописывается в конец клавиатур
`/start` и `/help` (`utils/inline_kb.with_inline_hint`): по нажатию юзер
выбирает чат, и inline открывается в нём с подставленным `@<bot>`.
`chosen_chat`, а не `current_chat`, потому что `/start` приходит в личку
с ботом — `current_chat` открыл бы inline в диалоге с самим ботом.

**`cache_time` — только из `config.INLINE_CACHE_*`.** Литерал в
`cache_time=` означает забытый `config` (ловит `tests/test_inline.py`).

Покрыто `tests/test_inline.py`: один ответ на запрос (иначе
`BadRequest` от двух `inline.answer()`), уникальность `id` ≤ 64
символов, экранирование, маршрутизация `@help`/`@status`.

---

## Команды

Полный список алиасов — `utils/cmds.py` (`CMDS`, 101 штука). Пер-command
справка — `handlers/commands/_helpdb.py` (`HELPS`, `_PAIRS`); расхождение
между ними ловит `tests/test_command_registry.py`.

### Telethon-only (нет aiogram-роутера)

| Команда | Алиасы |
|---|---|
| `.del` | `.удалить` |
| `.tagall` | `.тегвсех`, `.все` |
| `.влс` | `.vls`, `.лс`, `.dm` |
| `.admins` | `.админы` |
| `.pin` | `.закрепить`, `.закреп` |
| `.unpin` | `.открепить`, `.раскрепить`, `.откреп` |
| `.ссылка` | `.invitelink`, `.инвайт`, `.invite` |
| `.quote` | `.цитата`, `.q`, `.цит` |
| `.шаб` | `.шаблон`, `.template`, `.tmpl`, `.tpl` (+ `+шаб`/`-шаб` и их алиасы) |
| `.ня` | — |
| `.вгф` | `.vfg`, `.gif` |

### Требующие Telethon-сессии

`.watch`/`@username`, сохранение view-once, `.ии ctx=N`, `.ии дебаг`,
`.tr auto`.

### Модули юзера

Пользовательский код может добавлять свои команды. Реестр — `101 штука`
**не считает** их: `utils/cmds.py` остаётся листовым статическим
словарём, а модули живут в отдельном реестре `utils/modules.py`.

| Что | Где живёт | Как подключено |
|---|---|---|
| dot-команды в чатах | `_index[head][uid]` | `TelethonManager._handle_modules` — до системной цепочки |
| dot-команды в личке | тот же индекс | `handlers/commands/modules.py::dispatch_module_command` |
| inline-команды | `_registry[uid][name].inline` | `handlers/inline/__init__.py::_try_module_inline` |
| aiogram-хендлеры | `router` самого модуля | подключается в цепочку `handlers/commands` |

Полное описание API, ограничений и модели доверия — **`modules_dev.md`**.

Три правила, которые нельзя нарушать:

1. **Изоляция по `uid`.** Команда модуля видна только той сессии,
   которая его установила: индекс ключуется по `(head, uid)`, а не по
   `head`. Одинаковое имя команды у двух юзеров — не конфликт.
2. **Установка ≠ замена.** Модуль, объявивший системную команду
   (`.ping`), не перехватывает её, пока юзер явно не выбрал «заменить»
   в карточке конфликта. Отключённая системная команда молчит, а не
   подсказывает «возможно, это .ping?».
3. **Гейт доступа.** `MODULE_ALLOWLIST` проверяется в трёх точках:
   загрузка модуля, вызов хендлера, UI. Пустой список = открытый
   режим (WARNING в лог). Песочницы нет by design — см. `modules_dev.md`.

---

## Структура файлов

```
bot.py                  # точка входа: Dispatcher + Telethon в on_startup
config.py               # env-конфиг, секреты только через .env
slimbot_api.py          # публичный API для авторов модулей (реэкспорт)
AGENTS.md  TODO.md  CHANGELOG.md  README.md
modules_dev.md          # документация для авторов модулей

# состояние (в DATA_DIR, путь из SLIMBOT_DATA_DIR)
user_sessions.json  watched_chats.json  photo_settings.json
auto_tr_chats.json  user_timezones.json  nya_chats.json
templates.json  knowledge_settings.json  knowledge.sqlite3
ai_memory.json  templates/  sessions/  temp/  bot.log

utils/
  __init__.py __init__ exports
  cmds.py               # РЕЕСТР алиасов + бюджеты rate-limit
  shared_cmd.py         # логика команд, общая для обоих путей
  tlm_common.py         # thread_id_of / chat_link / usernames_of
  tlm_rpc.py            # RPC-обёртки с таймаутом
  telethon_manager.py   # TelethonManager, диспатч dot-команд, formatters
  storage.py            # JSON-хранилища, was_processed, session_path
  texts.py  escape.py   # шаблоны текстов, HTML-экранирование
  premium.py  bot_info.py  rate_limit.py  rate_limit_gate.py
  ai.py  ai_memory.py  ai_prompts.py
  knowledge_db.py  knowledge_collector.py
  calc.py  hashing.py  catgirl.py  timezones.py  suggest.py  media_kind.py
  linkcheck.py  netinfo.py  url_safety.py  logging_setup.py
  quote_image.py  gif_converter.py
  inline_kb.py           # connect_button + switch_inline_markup
  modules.py             # реестр модулей: загрузка, изоляция, конфликты
  module_api.py          # @command / @inline / Context для авторов модулей
  module_state.py        # ctx.state — per-user JSON состояние модуля

handlers/
  errors.py             # logger.exception для необработанных ошибок
  session.py            # auth-flow + SESSION_ALLOWLIST
  commands/
    __init__.py         # порядок роутеров + commands-fallback
    _base.py            # command_card, _truncate, render_time/id/help
    _helpdb.py          # HELPS, ALIASES/_PAIRS, is_help_request
    cmdhelp.py extra.py help.py id.py ping.py time.py timezone.py
    love.py coin.py start.py status.py logout.py
    netcmds.py tools.py ai.py knowledge.py opencode.py watch.py
    modules.py            # /modules + приём .py + команды модулей в личке
    # Telethon-only (без Router)
    admins.py  pin.py  invitelink.py  delmsg.py  dm.py
    tagall.py  nya.py   vgf.py         quote.py  template.py
  inline/
    __init__.py         # dispatch_inline — единственный inline_query
    help.py  status.py  profile.py

tests/                  # 926 тестов
```

---

## Инварианты (покрыты тестами)

1. У `handlers.commands.router` **ноль** собственных message-хендлеров.
2. `commands-fallback` — последний суб-роутер; его хендлеры поднимают
   `SkipHandler`.
3. Каждый `was_processed(...)` передаёт `user_id`.
4. В карточке ровно один `<blockquote>`; длина ≤ 4096 **по UTF-16**.
5. Ни одной мёртвой константы в `utils/texts.py`.
6. Алиасы команд ↔ `_helpdb` ↔ реестр согласованы в обе стороны.
7. Голых `client.get_entity(...)` / `get_me()` без `wait_for` нет.
8. Async-код не вызывает синхронный `knowledge_db.*` напрямую.
9. ffmpeg-хелперы убивают процесс и на `TimeoutError`, и на `CancelledError`.
10. CPU-bound (Pillow/rlottie) уходит в поток.
11. Файлы состояния валидируются при загрузке — битый JSON не роняет импорт
    и не ломает view-once.
12. У inline-роутера ровно один `inline_query`-хендлер; сабмодули
    `handlers/inline/*` не регистрируют свои.
13. `cache_time` в `handlers/inline/*` — только `config.INLINE_CACHE_*`.
14. Команда модуля исполняется только для `uid`, который её установил:
    индекс `_index[head][uid]` и `ModuleState(uid, module)` никогда не
    переходят между сессиями.
15. Модуль не перехватывает системную команду без явного решения юзера
    («заменить» в карточке конфликта).
16. Роутер модуля подключён через `include_router` (а не вставкой в
    `sub_routers`): `Dispatcher` обходит событие по `parent_router`,
    и роутер без этой связи выглядит подключённым, но не обходится.
17. Ни один хендлер на `handlers.commands` не ловит событие «на
    автомате»: хендлер, вернувший не-`UNHANDLED`, останавливает
    обход всей цепочки. Ранние выходы — только `SkipHandler`.
