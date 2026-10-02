# Slim Bot

Telegram-бот на движке **Telethon** (MTProto): dot-команды в любых чатах,
сохранение одноразовых фото, резолв `@username`, авто-перевод,
ИИ-ассистент (`.ии`) с памятью диалогов и self-tool-use. Управление —
через aiogram Bot API в личке с ботом (`/start`, auth-кнопки, inline-режим).

Подробная архитектура — в [AGENTS.md](AGENTS.md).

## Быстрый старт

```bash
cp .env.example .env && chmod 600 .env && nano .env   # заполнить Tier 1
pip install -r requirements.txt
python3 bot.py
```

Обязательные переменные (Tier 1): `TOKEN` (BotFather), `API_ID` / `API_HASH`
(my.telegram.org), `AI_API_KEY_WILLOW` (LLM для `.ии`). Остальное —
опциональные тюнинги со встроенными defaults (см. `.env.example`, `config.py`).

Для публичного бота настоятельно рекомендуется `SESSION_ALLOWLIST` — без него
подключить свой аккаунт может кто угодно, и все сессии делят один LLM-ключ.

Для картинок нужен Pillow (в `requirements.txt`); для `.вгф` из видео —
внешний `ffmpeg` в `PATH` (`apt-get install -y ffmpeg`).

## Команды

- `.ping .time .id .me .chat .who .net .hash .uuid .b64 .tr .calc .love .coin`
- `.govno` — анимация
- `.watch/.unwatch/.watched`, `.timezone`, `.ии`, `.save`, `.del`, `.pin/.unpin`,
  `.tagall`, `.влс`, `.admins`, `.ссылка`, `.quote`, `.ня`, `.вгф`, `.опенкод`
- `.q/.цитата` — оформить сообщение картинкой-цитатой (кружки, видео, стикеры,
  анимированные Premium-эмодзи)
- `.шаб/.+шаб/.-шаб` — шаблоны сообщений: сохранить любое сообщение под именем
  и отправлять потом одной командой (11 типов, список с пагинацией)
- `.ии база` — поиск по переписке (локальный SQLite-индекс)
- `.команда справка` — помощь по любой команде; `/start /help /status /logout` — в личке

Полный список алиасов — `utils/cmds.py`.

## Тесты

```bash
python3 -m pytest tests/ -q
```

Тесты покрывают в том числе инварианты, которые иначе тихо ломаются:
маршрутизация aiogram, изоляция состояния по `user_id`, отсутствие вложенных
`<blockquote>`, троттлинг по UTF-16, согласованность `AGENTS.md` и справки
с кодом.

## Docker

```bash
docker compose up --build -d
```

Состояние (сессии, JSON, логи) живёт в volume `slimbot-data`
(`SLIMBOT_DATA_DIR=/data`). Логи: `docker compose logs -f` + `bot.log`
с ротацией 10МБ×5 внутри контейнера.
