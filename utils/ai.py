"""Запросы к ddt-прокси (OpenAI-совместимый `/v1/chat/completions`).

Поддержка: текст, контекст, анализ изображений (vision).
Backend, модель, группа и effort задаются только в `.env` через config.py.

Три отличия прокси от обычного OpenAI:

1. **Заголовок `X-DDT-Group`** — приоритет группы, а не фильтр: если в
   группе нет ключа под запрошенную модель, прокси отправит запрос в
   другую группу сам. Поэтому `gemini-3.8-flash` при группе `GPT Plus`
   работает, хотя в этой группе только gpt-*.
2. **`max_retries = 0`** — ретраи делает прокси. Клиентские ретраи
   дублировали бы работу и жгли лимиты, поэтому ниже их ровно два:
   попытка в пределах модели и переход к запасной.
3. **Ответ содержит `model`** — по нему видно, какая модель ответила
   на самом деле. Возвращаем это вызывающему, чтобы `.ии` подписывала
   ответ (`AI_SHOW_MODEL`).
"""

import base64
import asyncio
import json
import time
import aiohttp

from config import (
    AI_API_KEY,
    AI_GROUP,
    AI_MODEL,
    AI_MODEL_ATTEMPTS,
    AI_MODEL_FALLBACK,
    AI_REASONING_EFFORT,
    AI_URL,
    LLM_TIMEOUT as TIMEOUT,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
)
# Системные промпты (большие, часто тюнятся) живут в ai_prompts.py.
# Старое имя SYSTEM_PROMPT оставлено как алиас для back-compat (AGENTS.md, логи).
from utils.ai_prompts import SYSTEM

SYSTEM_PROMPT = SYSTEM


def _headers() -> dict:
    """Заголовки запроса к прокси.

    `X-DDT-Group` шлём только если группа задана: лишний заголовок с
    пустым значением — лишний мусор в логах прокси.
    """
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    if AI_GROUP:
        headers["X-DDT-Group"] = AI_GROUP
    return headers


def _models_chain() -> list[str]:
    """Модели для перебора: основная, затем фолбэк (без дублей)."""
    chain = [AI_MODEL]
    if AI_MODEL_FALLBACK and AI_MODEL_FALLBACK != AI_MODEL:
        chain.append(AI_MODEL_FALLBACK)
    return chain[:AI_MODEL_ATTEMPTS]


def _model_label(model: str | None) -> str:
    """Человеческое имя модели для подписи ответа."""
    if not model:
        return AI_MODEL
    # Прокси может вернуть `gemini-3.8-flash` или с префиксом группы —
    # показываем ровно то, что ответило, но без мусора вида `openai/`.
    return str(model).rsplit("/", 1)[-1]


def _extract_error(body: str, limit: int = 300) -> str:
    """Достать человеческий текст ошибки из тела ответа.

    Прокси возвращает `{"error": {...}}`, но не всегда: иногда прилетает
    HTML от nginx или голый текст. Поэтому порядок такой — сначала
    пробуем JSON, и если не вышло, режем тело как есть.
    """
    raw = (body or "")[:4096]
    try:
        parsed = json.loads(raw)
    except ValueError:
        return (body or "")[:limit]
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict) and "message" in err:
            return str(err["message"])[:limit]
        if isinstance(err, str):
            return err[:limit]
        if "message" in parsed:
            return str(parsed["message"])[:limit]
    return (body or "")[:limit]


async def ask(
    prompt: str,
    context: list[dict] | None = None,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
    *,
    include_user_message: bool = True,
    system_override: str | None = None,
    return_usage: bool = False,
) -> tuple[str, str | None] | tuple[str, str | None, dict]:
    """Отправляет запрос к LLM.

    - prompt: текст промпта. Используется только если include_user_message=True.
    - context: список предшествующих сообщений [{"role":..., "content":...}].
    - image_bytes: если передано И include_user_message=True — последний user-месседж
      будет multimodal (текст + картинка).
    - include_user_message: если False, функция НЕ добавляет финальный user-turn.
      Используется при циклическом tool-loop'е (см. _llm_iterate в
      handlers/commands/ai.py): caller сам накапливает messages, иначе был бы
      дубликат user-role на каждой итерации.
    - system_override: если задан — заменяет дефолтный SYSTEM_PROMPT целиком.
      Используется отдельными фичами (например `.tr ai`), которым нужен
      совсем другой системный промпт, а не общий ассистентский (с тулами и т.п.).

    Возвращает `(ответ, ошибка)` или `(ответ, ошибка, usage)`, где usage
    содержит `model` — какая модель ответила на самом деле.
    """
    started = time.monotonic()

    def result(text: str, error: str | None, usage: dict | None = None):
        if return_usage:
            return text, error, usage or {}
        return text, error

    messages = [{"role": "system", "content": system_override or SYSTEM_PROMPT}]
    if context:
        messages.extend(context)

    if include_user_message:
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode()
            data_uri = f"data:{image_mime};base64,{b64}"
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            })
        else:
            messages.append({"role": "user", "content": prompt})

    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
        "stream": False,
    }
    if AI_REASONING_EFFORT:
        payload["reasoning_effort"] = AI_REASONING_EFFORT
    headers = _headers()
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    chain = _models_chain()
    last_error = ""
    data: dict = {}

    for model in chain:
        payload["model"] = model
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(AI_URL, json=payload, headers=headers) as r:
                    if r.status != 200:
                        body = await r.text()
                        clean_body = _extract_error(body)
                        hint = ""
                        if r.status == 401:
                            hint = ("<br><br><b>Подсказка:</b> прокси отверг ключ из "
                                    "<code>AI_API_KEY_DDT</code>.")
                        elif r.status == 429:
                            hint = "<br><br><b>Подсказка:</b> rate limit — подожди и повтори."
                        elif r.status >= 500:
                            hint = "<br><br><b>Подсказка:</b> backend недоступен — пробуем другую модель."
                        last_error = (
                            f"[x] HTTP {r.status} ({model}): {clean_body}{hint}"
                        )
                        # 4xx (кроме 429) — запрос плохой, фолбэк не поможет.
                        if 400 <= r.status < 500 and r.status != 429:
                            return result("", last_error)
                        continue
                    try:
                        data = await r.json(content_type=None)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        last_error = f"[x] Прокси вернул некорректный JSON ({model})"
                        continue
                    break
        except asyncio.TimeoutError:
            last_error = f"[x] Прокси не ответил вовремя ({model})"
            continue
        except aiohttp.ClientClientError as e:
            last_error = f"[x] Сетевая ошибка прокси: {type(e).__name__}"
            continue
        except Exception as e:
            last_error = f"[x] Ошибка прокси: {type(e).__name__}"
            continue
    else:
        return result("", last_error or "[x] Прокси не вернул ответ")

    try:
        msg = data["choices"][0]["message"]
        # Reasoning-модели могут положить финальный ответ
        # в reasoning_content, а content остаётся пустым.
        # Берём content если он непустой, иначе fallback на reasoning_content.
        text = msg.get("content") or msg.get("reasoning_content") or ""
        if not isinstance(text, str):
            return result("", "[x] Прокси вернул ответ без текста")
    except (KeyError, IndexError, TypeError):
        return result("", f"[x] Некорректный ответ API: {str(data)[:200]}")

    raw_usage = data.get("usage") if isinstance(data, dict) else None
    usage = {
        "input_tokens": int((raw_usage or {}).get("prompt_tokens", 0) or 0),
        "output_tokens": int((raw_usage or {}).get("completion_tokens", 0) or 0),
        "seconds": round(time.monotonic() - started, 2),
        # Какая модель ответила на самом деле: прокси возвращает `model`
        # в ответе, и приоритет группы может увести запрос в другую группу.
        "model": _model_label(data.get("model") if isinstance(data, dict) else None),
    }
    return result(text.strip(), None, usage)
