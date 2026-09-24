"""Подбор ближайшей команды при опечатке.
Использует все алиасы из handlers.commands._helpdb._PAIRS как словарь валидных команд."""


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _build_index():
    from handlers.commands._helpdb import _PAIRS
    index = {}
    for key, aliases in _PAIRS.items():
        canonical = "." + aliases[0]
        for a in aliases:
            index[a] = canonical
    return index


_index_cache: dict[str, str] | None = None


def _get_index() -> dict[str, str]:
    global _index_cache
    if _index_cache is None:
        _index_cache = _build_index()
    return _index_cache


def suggest(raw_head: str, max_distance: int = 2) -> str | None:
    """raw_head — уже с точкой, например '.пенг'.
    Возвращает canonical команду (английский алиас) с точкой либо None."""
    if not raw_head or not raw_head.startswith("."):
        return None
    name = raw_head[1:].lower()
    if not name:
        return None
    index = _get_index()
    if name in index:
        return None
    aliases = list(index.keys())
    best = None
    best_d = max_distance + 1
    for alias in aliases:
        if abs(len(alias) - len(name)) > max_distance:
            continue
        d = _levenshtein(name, alias)
        if d < best_d:
            best_d = d
            best = alias
            if d == 1:
                break
    if best is None or best_d > max_distance:
        return None
    return index[best]


def suggest_text(raw_head: str) -> str | None:
    """Готовая строка-подсказка для ответа пользователю."""
    canonical = suggest(raw_head)
    if not canonical:
        return None
    return f"[?] Команда не найдена. Возможно, это <code>{canonical}</code>?"
