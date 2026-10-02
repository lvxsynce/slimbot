"""Per-command help. Триггер: <code>.cmd справка</code> или <code>.cmd help</code>."""

# (рус_алиасы, англ_алиасы): {syntax, desc}
HELPS: dict[str, dict[str, str]] = {
    "ping": {
        "syntax": ".ping  |  .пинг",
        "desc": "Замер задержки между отправкой и обработкой.",
    },
    "time": {
        "syntax": ".time  |  .время",
        "desc": "Текущее время в твоей таймзоне (по умолчанию UTC; меняется через <code>.timezone</code>).",
    },
    "id": {
        "syntax": ".id  |  .инфо",
        "desc": "ID чата и твой ID.",
    },
    "me": {
        "syntax": ".me  |  .я",
        "desc": "Краткая инфа о тебе.",
    },
    "chat": {
        "syntax": ".chat  |  .чат",
        "desc": "Краткая инфа о текущем чате.",
    },
    "who": {
        "syntax": ".who [reply]  |  .кто [reply]",
        "desc": "Подробная инфа об отправителе (нужен reply на сообщение).",
    },
    "help": {
        "syntax": ".help  |  .помощь",
        "desc": "Список всех команд.",
    },
    "love": {
        "syntax": ".love  |  .любовь",
        "desc": "Анимированное сердечко (3 цикла). После анимации команда удаляется автоматически.",
    },
    "govno": {
        "syntax": ".govno  |  .говно",
        "desc": "Анимированная какашка (как <code>.love</code>, но наоборот). Команда удаляется сама.",
    },
    "nya": {
        "syntax": ".ня [стоп|выкл|список]",
        "desc": (
            "Включить/выключить <b>catgirl-rewrite</b> всех твоих сообщений в этом чате. "
            "<br><br><b>Подкоманды:</b><br>"
            "• <code>.ня</code> — toggle ON/OFF в текущем чате<br>"
            "• <code>.ня стоп</code> / <code>.ня выкл</code> / <code>.ня off</code> — выключить<br>"
            "• <code>.ня список</code> / <code>.ня list</code> — список активных чатов<br><br>"
            "<b>Стиль rewrite:</b> заикание первой согласной (<code>но</code> → <code>н-н-но</code>), "
            "удвоение первой гласной (<code>ультра</code> → <code>у-у-ультра</code>), "
            "<code>*action*</code>-префикс, каомодзи-суффикс.<br><br>"
            "<i>Только с Telethon-сессией: бот должен редактировать ВАШИ исходящие сообщения через MTProto.</i>"
        ),
    },
    "watch": {
        "syntax": ".watch [@user|id]  |  .следить",
        "desc": "Отслеживать чат для сохранения одноразовых фото. Без аргумента — текущий чат (или текущий топик форума). С сессией: @username или -100... ID.",
    },
    "unwatch": {
        "syntax": ".unwatch  |  .хватит  |  .забыть",
        "desc": "Перестать отслеживать текущий чат.",
    },
    "watched": {
        "syntax": ".watched  |  .список",
        "desc": "Список отслеживаемых чатов.",
    },
    "net": {
        "syntax": ".net &lt;ip|domain|url&gt;",
        "desc": "Единый автоматический анализ IP, домена или URL: DNS (включая сверку у Cloudflare/Google/Quad9), Geo/ASN, статус, доступность сайта, title страницы, TLS, редиректы и пассивный аудит.",
    },
    "del": {
        "syntax": ".del [N]  |  .удалить",
        "desc": "Удалить N последних своих сообщений в чате (по умолчанию 1). Только с Telethon-сессией.",
    },
    "tr": {
        "syntax": ".tr [lang] &lt;text|reply&gt;  |  .перевод • .tr auto [lang]",
        "desc": "Перевод через нейросеть с сохранением стиля и сленга. Без lang — на русский. "
        "Пример: <code>.tr en привет</code>.\n\n"
        "С сессией: <code>.tr auto en</code> — AI-автоперевод своих исходящих сообщений в этом чате "
        "на английский, <code>.tr stop</code> — выключить, <code>.tr list</code> — список.",
    },
    "calc": {
        "syntax": ".calc &lt;expr&gt;  |  .калк",
        "desc": "Калькулятор: + - * / ** % // ( ). Работает с пробелами: 2 + 2 * 3.",
    },
    "save": {
        "syntax": ".save [reply]  |  .сохранить",
        "desc": "Переслать сообщение в «Избранное». Только с Telethon-сессией.",
    },
    "logout": {
        "syntax": "/logout",
        "desc": "Выйти из Telethon-сессии (отозвать в Telegram).",
    },
    "tagall": {
        "syntax": ".tagall [текст]  |  .все",
        "desc": "Тегнуть всех в группе. Только с Telethon-сессией.",
    },
    "dm": {
        "syntax": ".влс [текст]  |  .vls  |  .лс  |  .dm",
        "desc": "Отправить текст в личку автору сообщения (нужен reply). Без текста — «.». Только с Telethon-сессией.",
    },
    "hash": {
        "syntax": ".hash &lt;алг&gt; &lt;text|reply&gt;  |  .хеш",
        "desc": "Хеш текста или файла по reply (md5, sha1, sha224, sha256, sha384, sha512, sha3_256, sha3_512, blake2b, blake2s). "
                "Если reply на документ/фото/видео/аудио — хеш скачанного файла.",
    },
    "uuid": {
        "syntax": ".uuid [N]  |  .юид",
        "desc": "До 20 UUIDv4 за раз. <code>.uuid</code> — один, <code>.uuid 5</code> — пять.",
    },
    "admins": {
        "syntax": ".admins  |  .админы",
        "desc": "Список администраторов текущего чата. Только с Telethon-сессией.",
    },
    "pin": {
        "syntax": ".pin [silent|loud]  |  .закрепить  |  .закреп",
        "desc": "Закрепить сообщение (reply). Без reply — закрепляет текущее. Флаг silent/loud управляет уведомлением. Только с Telethon-сессией.",
    },
    "unpin": {
        "syntax": ".unpin [all]  |  .открепить  |  .раскрепить",
        "desc": "Снять закрепление. С reply — с указанного сообщения. Без аргументов — с последнего закреплённого. `all` — снять все. Только с Telethon-сессией.",
    },
    "invitelink": {
        "syntax": ".ссылка  |  .invitelink  |  .инвайт",
        "desc": "Получить инвайт-ссылку на текущий чат. Только с Telethon-сессией (нужны права админа).",
    },
    "coin": {
        "syntax": ".монетка  |  .coin  |  .орёл  |  .решка",
        "desc": "Подбросить монетку — орёл или решка.",
    },
    "ai": {
        "syntax": ".ии <запрос>  |  .ии база [вопрос]  |  .ии сброс",
        "desc": (
            "ИИ-ассистент. Модель подписывается в футере ответа.<br><br>"
            "<b>Команды:</b><br>"
            "• <code>.ии &lt;запрос&gt;</code> — задать вопрос (память диалога)<br>"
            "• <code>.ии (reply)</code> — вопрос к сообщению / анализ фото<br>"
            "• <code>.ии сброс</code> — очистить историю чата<br>"
            "• <code>.ии сброс все</code> — очистить все чаты<br>"
            "• <code>.ии база</code> — в личке: выбрать чаты кнопками и запустить сбор<br>"
            "• <code>.ии база &lt;вопрос&gt;</code> — спросить по выбранным чатам<br>"
            "• Статус и пауза — кнопками в меню базы<br>"
            "• <code>.ии дебаг &lt;запрос&gt;</code> — список тулов (только в чатах)<br><br>"
            "Настройки backend'а задаются на сервере, пользователи не переключают их."
        ),
    },
    "opencode": {
        "syntax": ".опенкод  |  .opencode",
        "desc": "Статистика OpenCode: сообщения, токены (ввод/выход/кэш) за день и неделю с разбивкой по моделям и провайдерам.",
    },
    "timezone": {
        "syntax": ".timezone [значение]  |  .таймзона  |  .tz",
        "desc": "Часовая зона для <code>.time</code>. Без аргумента — текущая + пресеты.\n"
                "Значение: <code>+3</code>, <code>-5</code>, <code>+5:30</code>, <code>Europe/Moscow</code>, "
                "или алиас <code>МСК</code>/<code>киев</code>/<code>токио</code>.\n"
                "Сброс: <code>UTC</code> / <code>reset</code> / <code>сброс</code>.",
    },
    "b64": {
        "syntax": ".b64 текст  |  .base64",
        "desc": "Base64 encode (по умолчанию) или decode (<code>.b64 decode ...</code>). "
                "Можно реплаем на сообщение.",
    },
    "quote": {
        "syntax": ".quote  |  .цитата  |  .q  |  .цит",
        "desc": "Красиво оформить replied сообщение В ВИДЕ PNG-картинки (1400px): timestamp, sender, "
                "текст, forward-info. Фон — фото из реплая (blur). Видео/кружки/GIF в реплае → анимированная "
                "GIF-цитата (кружок — круглой маской). Голосовое/аудио в реплае → mp4-цитата со звуком. "
                "Анимированные Premium-эмодзи (TGS/WEBM) рисуются как inline-картинки и анимируются в GIF-цитате. "
                "Подпись: имя, фамилия, ID, username. Если Pillow/DejaVu Sans недоступны — graceful fallback на HTML blockquote. "
                "Только с Telethon-сессией.",
    },
    "template": {
        "syntax": ".шаб &lt;имя&gt; • .+шаб &lt;имя&gt; • .-шаб &lt;имя&gt; • .шаб список [стр]",
        "desc": (
            "Шаблоны сообщений: сохрани реплай и отправляй потом одним словом.<br><br>"
            "<b>Команды:</b><br>"
            "• <code>.шаб имя</code> — отправить шаблон<br>"
            "• <code>.+шаб имя</code> — сохранить сообщение, на которое ответил<br>"
            "• <code>.-шаб имя</code> — удалить шаблон<br>"
            "• <code>.шаб список [стр]</code> — список с пагинацией (напр. <code>.шаб список 2</code>)<br>"
            "• <code>.шаб</code> — справка<br><br>"
            "<b>Типы:</b> "
            "<tg-emoji emoji-id=\"5269564073464334732\">\U0001F4AC</tg-emoji> текст, "
            "<tg-emoji emoji-id=\"5987917196469213507\">\U0001F4F7</tg-emoji> фото, "
            "<tg-emoji emoji-id=\"5280662183057825163\">\U0001F3A5</tg-emoji> видео, "
            "<tg-emoji emoji-id=\"5280723506600911579\">✨</tg-emoji> GIF, "
            "<tg-emoji emoji-id=\"6032984490169605484\">⭕</tg-emoji> кружок, "
            "<tg-emoji emoji-id=\"5258500422393415126\">\U0001F3A4</tg-emoji> голосовое, "
            "<tg-emoji emoji-id=\"5287380220578372452\">\U0001F3A7</tg-emoji> аудио, "
            "<tg-emoji emoji-id=\"60125290227634244\">\U0001F308</tg-emoji> стикер, "
            "<tg-emoji emoji-id=\"5341492148468465410\">\U0001F4C1</tg-emoji> документ, "
            "<tg-emoji emoji-id=\"5879770735999717115\">\U0001F464</tg-emoji> контакт, "
            "<tg-emoji emoji-id=\"5422548510740349734\">\U0001F4CD</tg-emoji> координаты.<br><br>"
            "Иконки типов — кастомные эмодзи, если у тебя Telegram Premium "
            "(иначе обычные юникод-символы). Имена регистронезависимы; "
            "<code>список</code>/<code>list</code> — зарезервированные.<br><br>"
            "<i>Только с Telethon-сессией.</i>"
        ),
    },
    "vgf": {
        "syntax": ".вгф [reply]  |  .vfg  |  .gif",
        "desc": (
            "Ответь на фото — бот сделает 2-кадровую GIF и отправит ответом на оригинал. "
            "Видео, кружки и анимированные фото конвертируются в настоящую анимированную GIF "
            "(ffmpeg, до 8 с, ширина до 720px). "
            "Telegram auto-конвертирует multi-frame GIF в inline-playable preview.<br><br>"
            "Требует ffmpeg в PATH. Только с Telethon-сессией."
        ),
    },
}


# Карта алиас → ключ. Сюда же добавляются и сами имена без точки.
ALIASES: dict[str, str] = {}
_PAIRS = {
    "ping": ["ping", "пинг"],
    "time": ["time", "время"],
    "id": ["id", "инфо"],
    "me": ["me", "я"],
    "chat": ["chat", "чат"],
    "who": ["who", "кто"],
    "help": ["help", "помощь"],
    "love": ["love", "любовь"],
    "govno": ["govno", "говно"],
    "nya": ["ня"],
    "vgf": ["вгф", "vfg", "gif"],
    "watch": ["watch", "следить"],
    "unwatch": ["unwatch", "хватит", "забыть"],
    "watched": ["watched", "список"],
    "net": ["net", "сеть", "сет"],
    "del": ["del", "удалить"],
    "tr": ["tr", "перевод", "пер", "перевести"],
    "calc": ["calc", "калк"],
    "save": ["save", "сохранить"],
    "logout": ["logout"],
    "tagall": ["tagall", "все", "тегвсех"],
    "dm": ["dm", "влс", "vls", "лс"],
    "hash": ["hash", "хеш", "хэш"],
    "uuid": ["uuid", "юид"],
    "admins": ["admins", "админы"],
    "pin": ["pin", "закрепить", "закреп"],
    "unpin": ["unpin", "открепить", "раскрепить", "откреп"],
    "invitelink": ["ссылка", "invitelink", "инвайт", "invite"],
    "coin": ["монетка", "coin", "монета", "орёл", "решка"],
    "quote": ["quote", "цитата", "q", "цит"],
    "template": [
        "шаб", "шаблон", "template", "tmpl", "tpl",
        "+шаб", "+шаблон", "+template", "+tmpl", "+tpl",
        "-шаб", "-шаблон", "-template", "-tmpl", "-tpl",
    ],
    "ai": ["ии", "ai", "ии?"],
    "timezone": ["timezone", "таймзона", "tz"],
    "b64": ["b64", "base64"],
    "opencode": ["опенкод", "opencode"],
}
for key, aliases in _PAIRS.items():
    for a in aliases:
        ALIASES[a] = key


HELP_WORDS = {"справка", "help", "?", "хелп"}


def is_help_request(text: str | None) -> tuple[bool, str | None]:
    """('.ping справка' → True, 'ping'). Иначе (False, None)."""
    if not text:
        return False, None
    parts = text.strip().split()
    if len(parts) < 2:
        return False, None
    head = parts[0].lstrip(".").lower()
    tail = parts[1].lower()
    if tail not in HELP_WORDS:
        return False, None
    key = ALIASES.get(head)
    return (key is not None), key


def render(key: str) -> str:
    info = HELPS.get(key)
    if not info:
        return f"<b>Slim bot | Help</b>\n<blockquote>[x] Нет справки по <code>{key}</code></blockquote>"
    return (
        "<b>Slim bot | Help</b>\n<blockquote>"
        f"<b>{info['syntax']}</b>\n"
        f"{info['desc']}"
        "</blockquote>"
    )
