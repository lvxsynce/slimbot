"""HTML-escape для user-controlled данных в командах (aiogram parse_mode=HTML).

Безопасный substitute для повсеместных `from html import escape as _h` —
раньше этот импорт повторялся в 10+ handlers. Один helper — single import.

quote=False по умолчанию — Telegram HTML не использует quote'ы в атрибутах
(`<a href=...>` достаточно эскейпить `<>&`). href-эскейп — `esc(url, quote=True)`.

NB: None → "" (НЕ "None"). Это защита от непреднамеренного info-disclosure
когда handler случайно вернёт None вместо строки.
"""
import html as _html
import re


def esc(s, /, *, quote: bool = False) -> str:
    """HTML-escape. None → "" (НЕ "None"). quote=True escape'ит также `\"'."""
    if s is None:
        return ""
    return _html.escape(str(s), quote=quote)


# ----------------- sanitize для LLM-вывода (Telegram HTML) -----------------

# Разрешённые теги (как в SYSTEM_PROMPT: b/i/u/s/code/a). blockquote НЕ
# разрешён — финальный ответ и так оборачивается в <blockquote>, а вложенный
# blockquote Telegram отвергает ("Can't nest entities of the same type").
_ALLOWED_TAG_RE = re.compile(r"</?(b|i|u|s|code|a)\b([^>]*)>$", re.IGNORECASE)
# Для <a> обязателен ровно один href-атрибут.
_A_ATTR_RE = re.compile(r"""^\s+href="[^"]+"\s*$""", re.IGNORECASE)
# Именованные HTML-entities, которые понимает Telegram parse_mode=HTML.
_ENTITIES = {"amp", "lt", "gt", "quot", "apos", "nbsp"}


def _is_numeric_entity(body: str) -> bool:
    """True для `123` (decimal) и `x41`/`X4A` (hex)."""
    if not body:
        return False
    if body[0] in "xX":
        hexpart = body[1:]
        return len(hexpart) > 0 and all(c in "0123456789abcdefABCDEF" for c in hexpart)
    return body.isdigit()


def sanitize_llm_html(text: str, max_len: int = 4000) -> str:
    """Приводит произвольный LLM-вывод к безопасному Telegram-HTML.

    Проблема: LLM может вернуть мусор — <pre>, markdown, несбалансированные
    <b> без закрытия, вложенные <blockquote>, сырые <>. Telegram с
    parse_mode=HTML отвечает BadRequest "Can't parse entities" → юзер видит
    вечный «[… ] Думаю…», а fallback-отправка падает повторно.

    Что делает:
    1. Обрезает до max_len (с отступом «…»).
    2. Все теги, кроме b/i/u/s/code/a (и <a href="...">) — экранирует в текст.
    3. Закрывает незакрытые теги; бросает stray closing-теги.
    4. Сохраняет известные entities, остальные & экранирует.
    """
    if not text:
        return ""
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "\n…"

    stack: list[str] = []
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "<":
            end = text.find(">", i + 1)
            if end == -1:  # незакрытый '<' — экранируем
                out.append("&lt;")
                i += 1
                continue
            raw = text[i:end + 1]
            m = _ALLOWED_TAG_RE.match(raw)
            if m:
                name = m.group(1).lower()
                attrs = m.group(2) or ""
                if raw.startswith("</"):
                    if stack and stack[-1] == name:
                        stack.pop()
                        out.append(raw)
                    else:
                        out.append("")  # stray closing-тег — выбрасываем
                else:
                    if name == "a":
                        if not _A_ATTR_RE.match(attrs):
                            out.append(_html.escape(raw, quote=True))
                        else:
                            stack.append(name)
                            out.append(raw)
                    elif attrs.strip():
                        # b/i/u/s/code с атрибутами (<b onclick="x">) —
                        # экранируем целиком: Telegram-атрибутов у них нет.
                        out.append(_html.escape(raw, quote=True))
                    else:
                        stack.append(name)
                        out.append(raw)
            else:
                out.append(_html.escape(raw, quote=True))
            i = end + 1
        elif ch == "&":
            j = text.find(";", i + 1)
            if j != -1 and 2 <= j - i <= 8:
                ent = text[i + 1:j]
                num = ent.startswith("#") and _is_numeric_entity(ent[1:])
                if ent in _ENTITIES or num:
                    out.append(text[i:j + 1])
                    i = j + 1
                    continue
            out.append("&amp;")
            i += 1
        else:
            out.append(ch)
            i += 1

    for name in reversed(stack):
        out.append(f"</{name}>")
    return "".join(out)
