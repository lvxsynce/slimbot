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

Для картинок нужен Pillow (в `requirements.txt`); для `.вгф` из видео —
внешний `ffmpeg` в `PATH` (`apt-get install -y ffmpeg`).

## Команды

- `.ping .time .id .me .chat .who .net .hash .uuid .b64 .tr .calc .love .coin`
- `.watch/.unwatch/.watched`, `.timezone`, `.ии`, `.save`, `.del`, `.pin/.unpin`,
  `.tagall`, `.влс`, `.admins`, `.ссылка`, `.quote`, `.ня`, `.вгф`
- `.команда справка` — помощь по любой команде; `/start /help /status /logout` — в личке.

## Тесты

```bash
python3 -m pytest tests/ -q
```
