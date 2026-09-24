"""Запросы к фиксированному Willow API.

Поддержка: текст, контекст, анализ изображений (vision).
Backend, модель и effort задаются только в `.env` через config.py.
"""

import base64
import asyncio
import json
import time
import aiohttp

from config import (
    AI_API_KEY,
    AI_MODEL,
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

    Возвращает (ответ_текст, error_or_None).
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
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(AI_URL, json=payload, headers=headers) as r:
                    if r.status != 200:
                        body = await r.text()
                        clean_body = body[:300]
                        try:
                            parsed = json.loads(body[:4096])
                            if isinstance(parsed, dict) and "error" in parsed:
                                err_obj = parsed["error"]
                                if isinstance(err_obj, dict) and "message" in err_obj:
                                    clean_body = str(err_obj["message"])[:300]
                                elif isinstance(err_obj, str):
                                    clean_body = err_obj[:300]
                        except ValueError:
                            pass
                        hint = ""
                        if r.status == 401:
                            hint = "<br><br><b>Подсказка:</b> Willow API отверг ключ из <code>AI_API_KEY_WILLOW</code>."
                        elif r.status == 429:
                            hint = "<br><br><b>Подсказка:</b> rate limit — подожди и повтори."
                        elif r.status >= 500:
                            hint = "<br><br><b>Подсказка:</b> backend недоступен — попробуй позже."
                            if attempt == 0:
                                await asyncio.sleep(0.4)
                                continue
                        return result("", f"[x] HTTP {r.status} (Willow/{AI_MODEL}): {clean_body}{hint}")
                    try:
                        data = await r.json(content_type=None)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        return result("", "[x] Willow API вернул некорректный JSON-ответ")
                    break
        except asyncio.TimeoutError:
            if attempt == 0:
                continue
            return result("", "[x] Willow API не ответил вовремя")
        except aiohttp.ClientError as e:
            if attempt == 0 and isinstance(e, (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError)):
                await asyncio.sleep(0.4)
                continue
            return result("", f"[x] Сетевая ошибка Willow API: {type(e).__name__}")
        except Exception as e:
            return result("", f"[x] Ошибка Willow API: {type(e).__name__}")
    else:
        return result("", "[x] Willow API не вернул ответ")

    try:
        msg = data["choices"][0]["message"]
        # Reasoning-модели могут положить финальный ответ
        # в reasoning_content, а content остаётся пустым.
        # Берём content если он непустой, иначе fallback на reasoning_content.
        text = msg.get("content") or msg.get("reasoning_content") or ""
        if not isinstance(text, str):
            return result("", "[x] Willow API вернул ответ без текста")
    except (KeyError, IndexError, TypeError):
        return result("", f"[x] Некорректный ответ API: {str(data)[:200]}")

    raw_usage = data.get("usage") if isinstance(data, dict) else None
    usage = {
        "input_tokens": int((raw_usage or {}).get("prompt_tokens", 0) or 0),
        "output_tokens": int((raw_usage or {}).get("completion_tokens", 0) or 0),
        "seconds": round(time.monotonic() - started, 2),
    }
    return result(text.strip(), None, usage)
