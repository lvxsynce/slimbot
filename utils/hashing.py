"""Хэш, UUID и base64 — stdlib only."""
import base64
import hashlib
import uuid

ALGOS = ("md5", "sha1", "sha224", "sha256", "sha384", "sha512", "sha3_256", "sha3_512", "blake2b", "blake2s")


def hash_text(algo: str, text: str) -> str:
    algo = algo.lower()
    if algo not in ALGOS:
        return f"[x] Алгоритм неизвестен. Доступно: {', '.join(ALGOS)}"
    try:
        h = hashlib.new(algo)
        h.update(text.encode("utf-8"))
        return h.hexdigest()
    except Exception as e:
        return f"[x] {type(e).__name__}: {e}"


def hash_bytes(algo: str, data: bytes) -> str:
    """Хеш байтов (например, скачанного файла). Возвращает hex или '[x] …' при ошибке."""
    algo = algo.lower()
    if algo not in ALGOS:
        return f"[x] Алгоритм неизвестен. Доступно: {', '.join(ALGOS)}"
    if not isinstance(data, (bytes, bytearray)):
        return f"[x] Не байты: {type(data).__name__}"
    try:
        h = hashlib.new(algo)
        h.update(data)
        return h.hexdigest()
    except Exception as e:
        return f"[x] {type(e).__name__}: {e}"


def gen_uuid(version: int = 4) -> str:
    if version == 1:
        return str(uuid.uuid1())
    if version == 4:
        return str(uuid.uuid4())
    return "[x] Поддерживаются версии: 1, 4"


def gen_uuids(n: int) -> list[str]:
    """Batch генерация. n ограничен сверху (20) вызывающим кодом."""
    return [str(uuid.uuid4()) for _ in range(max(0, n))]


def b64_op(text: str, mode: str, url_safe: bool = False) -> str:
    """base64 encode/decode. Возвращает результат или '[x] …' при ошибке.

    url_safe=True → `urlsafe_b64encode/decode` — вариант с алфавитом
    '-_' вместо '+/'. Используется в JWT, URL-shortened payload'ах,
    file names. По умолчанию False (стандартный RFC 4648 base64 с '+/=').
    """
    try:
        if mode == "decode":
            if url_safe:
                # urlsafe decode не использует validate=True — base64.urlsafe_b64decode
                # тоже поддерживает malformed input (получает binascii.Error).
                decoded = base64.urlsafe_b64decode(text.encode("ascii"))
            else:
                decoded = base64.b64decode(text.encode("ascii"), validate=True)
            return decoded.decode("utf-8")
        if url_safe:
            return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
        return base64.b64encode(text.encode("utf-8")).decode("ascii")
    except UnicodeEncodeError as e:
        return f"[x] Текст не ASCII: {e}"
    except UnicodeDecodeError:
        return f"[x] Не UTF-8 после декодирования. Возможно, это файл (используй .save)."
    except Exception as e:
        return f"[x] {type(e).__name__}: {e}"
