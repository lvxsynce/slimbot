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

## Авторизация (важно!)
- На сервере НЕТ `gh`, НЕТ SSH-ключей GitHub, НЕТ сохранённых credentials (`git ls-remote origin` падает — это нормально)
- Пуш/ API только по HTTPS + PAT пользователя (`ghp_...`, scope `repo`). Токен НЕ хранить в файлах скилла и репозитория!
- Как брать токен: попросить пользователя вставить его сообщением в чат
- Как использовать, не светя в истории: сохранить в `/root/.gh_tok` (`chmod 600`), использовать через credential helper, после операции стереть (`rm -f`):
  ```
  printf '%s' '<TOKEN>' > /root/.gh_tok && chmod 600 /root/.gh_tok
  git -c credential.helper='!f() { echo username=x-access-token; echo "password=$(cat /root/.gh_tok)"; }; f' push origin <branch> --tags
  rm -f /root/.gh_tok
  ```
- API: `curl -H "Authorization: Bearer $(cat /root/.gh_tok)" https://api.github.com/...`
- Создание релиза: `POST /repos/lvxsynce/slimbot/releases` с `{"tag_name","name","body"}`
- Публичность: `PATCH /repos/lvxsynce/slimbot` с `{"private":false}`

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
