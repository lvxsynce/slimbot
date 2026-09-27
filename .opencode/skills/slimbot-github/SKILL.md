---
name: slimbot-github
description: GitHub-workflow проекта SlimBot (/root/slimbot → github.com/lvxsynce/slimbot). Используй при любых задачах про пуш, коммиты, теги, релизы, версии, CHANGELOG, ветки main/dev в этом проекте.
---

# SlimBot — GitHub workflow

## Репозиторий
- URL: `https://github.com/lvxsynce/slimbot` (публичный)
- Ветки: `main` (стабильная), `dev` (рабочая — все новые фичи/фиксы коммитить сюда)
- Версии: git-теги `vX.Y.Z` + GitHub Releases + `CHANGELOG.md` в корне
- Коммиты от имени: `user.name=slimbot`, `user.email=slimbot@local` (через `git -c`, глобальный конфиг не настроен)

## Авторизация (важно! read carefully)
- PAT scope `repo` СОХРАНЁН на сервере: `~/.git-credentials` (`chmod 600`, только root) + `git config --global credential.helper store`
- Поэтому `git push/pull/fetch/ls-remote origin` работают БЕЗ ручного ввода токена. Отдельно токен у пользователя НЕ запрашивать, если store работает (проверка: `git ls-remote origin HEAD`)
- Если store не срабатывает (смена токена/прав) — попросить новый PAT в чате и перезаписать `~/.git-credentials`, затем сразу проверить `git ls-remote`
- API (релизы и др.): токен читать программно из `~/.git-credentials` внутри скрипта, НИКОГДА не подставлять через `echo`/подстановки в строке команды и не печатать
- Создание релиза: `POST /repos/lvxsynce/slimbot/releases` с `{"tag_name","name","body"}`
- Публичность: `PATCH /repos/lvxsynce/slimbot` с `{"private":false}`

### Правила безопасности токена (строго!)
1. НИКОГДА не выводить токен в чат, логи, коммиты, код или вывод команд (`grep`, `cat ~/.git-credentials` — ЗАПРЕЩЕНЫ; `remote -v` — безопасен, токена там нет)
2. НИКОГДА не встраивать токен в remote URL (`git remote set-url` только чистый `https://github.com/...`)
3. НИКОГДА не коммитить файлы с токеном; перед каждым коммитом проверять `git status` / staged-скан
4. Токен — только для `origin` (github.com/lvxsynce/slimbot) и GitHub API этого репозитория; не использовать для других хостов/репо
5. Временные файлы с токеном (если понадобятся для curl): только `/root/`, `chmod 600`, удалять сразу после (`rm -f`)
6. При любом подозрении на утечку — сказать пользователю отозвать токен (Settings → Developer settings → Tokens) и выпустить новый

## Pre-push чеклист (обязательно каждый раз)
1. Секреты: в `config.py` только пустые defaults; живые `TOKEN`/`API_ID`/`API_HASH` — только в локальном `.env`. Скан staged-файлов по маскам секретов перед коммитом
2. `.gitignore` покрывает: `.env`, `*.log`, рантайм-JSON (`user_sessions` с телефонами и др.), `*.bak/*.tmp`, `*.session*`, `knowledge.sqlite3*`, `sessions/`, `temp/`, `qwengate-runtime/` (там `api.key`), `.claude/`
3. `python3 -m pytest tests/ -q` — всё зелёное
4. `.env.example` синхронизирован со всеми `os.getenv`/`_env` именами из `config.py` (+ `OPENCODE_*` из `opencode.py`)
5. `AGENTS.md` и `CHANGELOG.md` обновлены под изменения
6. Проверить `git add -A -n`: в набор не должны попасть `.env`, сессии, JSON-состояние, логи, `__pycache__`

## Релизный цикл
1. Работа в `dev` → merge в `main`
2. Тег `vX.Y.Z` на коммите `main` → `git push origin main --tags`
3. GitHub Release с описанием из CHANGELOG
4. Запись в `CHANGELOG.md` (секции Добавлено/Изменено/Исправлено)

## Связанное окружение (не коммитить, но знать)
- Рабочий каталог: `/root/slimbot`; сервис: `systemctl restart slimbot.service` после изменений кода (проверить `journalctl -u slimbot.service`)
- `/tmp` (tmpfs 982M) периодически забит мусором соседних проектов (`bds*`, `opencode_install_*`) — не трогать чужое, но знать при `No space left`
- Файл `opencode-install.sh` в репо НЕ нужен (был удалён как мусор, zero references)
