# Changelog

Все значимые изменения проекта. Формат: `Добавлено / Изменено / Исправлено`.

## [Unreleased]

## [1.1.0] — 2026-09-27
### Добавлено
- `.net`: сверка DNS у Cloudflare / Google / Quad9 / системного резолвера (с задержками)
- `.net`: проба доступности сайта (https → http, статус + время + Server)
- `.net`: title страницы для URL-целей (до 5 редиректов, кап 256 КБ, только HTML)
- `.net`: reverse DNS (PTR) для IP-целей; `.перевести` и `.хэш` как алиасы
- README.md, полный `.env.example` (все тюнинги)

## [1.0.0] — 2026-09-24
### Изменено
- Удаление Telegram Business API: `handlers/business.py`, `messages.py`, `deletions.py`,
  `connected_users`, кэш сообщений. Telethon — единственный исполнитель в чатах,
  aiogram Bot API — только управление (личка, auth, inline)
- Секреты только через `.env` (в репозитории живых значений нет)
### Исправлено
- Аудит ~60 находок: `Emoji.render` (пустые эмодзи), инвертированные guard'ы
  `.cmd справка` / `.me/.chat/.who`, 2FA без private-фильтра, FloodWait без retry,
  таймауты Telethon-вызовов, health-петля, `pow()`-обход лимитов калькулятора,
  хрупкие JSON-миграции, HTML-санитайзер, rate-limit `.ии`, даунскейл vision
- 81 тест (52 новых)
