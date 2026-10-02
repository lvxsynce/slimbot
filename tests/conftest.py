"""Общие фикстуры тестов.

Главное здесь — `tests/conftest.py` задаёт ФИКТИВНЫЕ секреты **до**
импорта `config`. Без этого набор тестов зависел от `.env`: пустой
`.env` (а он теперь пустой по назначению — секреты живут в
`/etc/slimbot/secrets.env`, см. Heroku-подход) ронял сборку на
`ValueError: invalid literal for int()`, а заполненный требовал бы
настоящих ключей на машине разработчика.

Тесты не должны требовать боевых доступов. Значения ниже — заглушки,
достаточные для проверки формы (bot-токен должен разбираться aiogram'ом
как `123:ABC`, `API_ID` — быть целым).
"""

import os

#: Фиктивные значения. Перекрывают `.env`, потому что `load_dotenv` по
#: умолчанию НЕ перезаписывает уже установленные переменные.
FAKE_ENV = {
    "TOKEN": "123456:TEST_TOKEN_FOR_LOCAL_DISPATCH_ONLY",
    "API_ID": "111111",
    "API_HASH": "0123456789abcdef0123456789abcdef",
    "AI_API_KEY_DDT": "test-ddt-token",
    "AI_API_URL_DDT": "http://127.0.0.1:9/v1",
    "AI_GROUP_DDT": "",
    "AI_MODEL_DDT": "test-model",
    "AI_MODEL_DDT_FALLBACK": "test-model-fallback",
    "OPENCODE_API_PASS": "",
    # Пустые allowlist'ы = открытый режим: гейты доступа проверяются
    # своими тестами через monkeypatch, а не зависят от окружения.
    "SESSION_ALLOWLIST": "",
    "MODULE_ALLOWLIST": "",
}

for _key, _value in FAKE_ENV.items():
    os.environ[_key] = _value

# Точка данных — во временный каталог: тесты не должны писать в рабочий
# DATA_DIR (иначе они засорят прод-состояние). Конкретные тесты, которым
# нужен свой DATA_DIR, переопределяют его через monkeypatch.
_TEST_DATA_DIR = "/tmp/slimbot-tests"
os.environ.setdefault("SLIMBOT_DATA_DIR", _TEST_DATA_DIR)

# Каталог создаём заранее: `config` только вычисляет пути, а `SESSIONS_DIR`
# открывает Telethon на первой же авторизации (`sqlite3.connect`), и без
# каталога это `unable to open database file`. Аналогично `TEMP_DIR`
# и каталог шаблонов — их тоже создают модули при первой записи.
for _sub in ("", "sessions", "temp", "templates", "modules", "module_state"):
    os.makedirs(os.path.join(_TEST_DATA_DIR, _sub), exist_ok=True)