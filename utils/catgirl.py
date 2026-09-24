"""Процедурный rewriter текста в стиле «кошко-девочка» для режима `.ня`.

Используется в utils/telethon_manager.py::_apply_nya для редактирования
исходящих сообщений юзера в чатах с включённым `.ня`. Никакого LLM —
только детерминированные правила, словари actions/kaomoji и псевдослучай.

ЕДИНОЕ ПРАВИЛО (по примерам пользователя — все паттерны first-char):

    "но"         → "н-н-но"    (consonant first, 3 reps через "-")
    "старая"     → "с-старая"  (consonant first, 2 reps — вариация через rng)
    "ультра"     → "у-у-ультра" (vowel first, всегда 3 reps)
    "ного"      → "н-н-него"  (consonant first)
    "У них"     → "У н-их"    (per-word first-char doubling)

ГАРАНТИЯ ВИДИМОГО ИЗМЕНЕНИЯ:
Если текст passes первичные guards (длина >= NYA_MIN_LEN и содержит русские
слова), результат ВСЕГДА отличается от original. Если probabilities по
stutter/vowel-double/action/kaomoji случайно сложились в 0 — на финальной
страховке ВСЕГДА добавляется kaomoji-suffix (100% гарантия видимого эффекта).

Раньше rewriter возвращал original когда результат идентичен — это вызывало
проблему «иногда `.ня` просто не редактирует». Теперь такого нет: rewriter
либо skip'ает (только если текст реально не подходит — пустой / не-русский /
слишком короткий), либо ВСЕГДА возвращает изменённый текст.

ЗАЩИТЫ:
- Минимальная длина текста (NYA_MIN_LEN) — короткие сообщения skip.
- Только русские слова матчатся regex'ом \\b[А-Яа-яЁё]{2,}\\b — латиница,
  цифры, URL, эмодзи, HTML-теги остаются нетронутыми.
"""

from __future__ import annotations

import random
import re

from config import (
    NYA_STUTTER_PROB,
    NYA_VOWEL_DOUBLE_PROB,
    NYA_ACTION_PROB,
    NYA_KAOMOJI_PROB,
    NYA_MIN_LEN,
)


# ----------------- Пул *action*-префиксов -----------------

CATGIRL_ACTIONS: tuple[str, ...] = (
    "*мурлычет себе под нос*",
    "*фыркает*",
    "*сонно зевает*",
    "*смущённо прячет мордочку в лапках*",
    "*тянется обнять*",
    "*подмигивает*",
    "*кружится вокруг*",
    "*моргает большими глазками*",
    "*поглаживает хвостиком*",
    "*распушает ушки*",
    "*тихонечко мурлычет*",
    "*машет хвостиком*",
    "*потягивается на солнышке*",
    "*прижимается к ножке*",
    "*шевелит ушками*",
    "*прыгает на месте от радости*",
    "*облизывает лапку*",
    "*довольно мурлычет, согреваясь*",
    "*смущённо опускает взгляд*",
    "*любопытно наклоняет головку*",
)


# ----------------- Пул каомодзи / суффиксов -----------------

CATGIRL_KAOMOJI: tuple[str, ...] = (
    "(⁄ ⁄•⁄ω⁄•⁄ ⁄)",
    "(๑˘◡˘๑)",
    "(ˆωˆ)",
    "にゃー",
    "(=^･ω･^=)",
    "ฅ^•ﻌ•^ฅ",
    "(=ω=)~",
    "♡",
    "✧",
    ">.<",
    "ฅ(´ω`ฅ)",
)


# ----------------- Regex / constants -----------------

# Граница слова: матчит standalone русский токен длины 2+ (одиночная "и"
# не превращается в "и-и-и"). URL/латиница/цифры не матчатся.
_WORD_RE = re.compile(r"\b[А-Яа-яЁё]{2,}\b", flags=re.UNICODE)

# Русские согласные, на которые можно «заикаться».
_STUTTER_CONSONANTS = set("бвгджзйклмнпрстфхцчшщ")
_STUTTER_CONSONANTS |= {c.upper() for c in _STUTTER_CONSONANTS}

# Гласные: для удвоения первой буквы, если слово начинается с гласной.
_RUS_VOWELS = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")

# Marker-suffixes для финальной страховки — используются когда ни одна
# декорация и ни одно заикание не сработали (теоретически возможно при
# неудачных rng-rolls подряд). ВСЕГДА даёт видимый эффект.
_FINAL_MARKERS: tuple[str, ...] = ("♡", "✧", "にゃー")


# ----------------- Unified first-char doubler -----------------

def _double_first_char(word: str, rng: random.Random) -> str:
    """Единая рула: удваиваем первую букву в зависимости от её типа.

    Consonant first → 2 или 3 reps через дефис (NYA_STUTTER_PROB gate).
      "но"     → "н-но"   /  "н-н-но"
      "старая" → "с-старая" / "с-с-старая"

    Vowel first → всегда 3 reps через дефис (NYA_VOWEL_DOUBLE_PROB gate),
    чтобы матчить пример пользователя "у-у-ультра" (3 буквы у, 2 дефиса).
      "ультра" → "у-у-ультра"

    Прочие символы (Ё/ё/цифры/etc) → skip.

    Применяется строго один раз per word — исключает гонку между правилами
    (в первой версии stutter + vowel-double давали мусор типа "н---но").
    """
    if len(word) < 2:
        return word
    first = word[0]
    if first in _STUTTER_CONSONANTS:
        if rng.random() > NYA_STUTTER_PROB:
            return word
        # 2 или 3 повтора через дефис — естественная вариативность.
        times = rng.choice((2, 3))
        return first + ("-" + first) * (times - 1) + word[1:]
    if first in _RUS_VOWELS:
        if rng.random() > NYA_VOWEL_DOUBLE_PROB:
            return word
        # Гласные: 3 повтора (итого 3 буквы, 2 дефиса) для матча с "у-у-ультра".
        return first + "-" + first + "-" + first + word[1:]
    # Ё/ё/цифры/символы — skip.
    return word


# ----------------- main entrypoint -----------------

def to_catgirl(text: str, rng: random.Random | None = None) -> str:
    """Процедурный rewriter в стиле catgirl.

    Возвращает NEW текст, ВСЕГДА отличающийся от original, если:
    - длина >= NYA_MIN_LEN;
    - текст содержит хотя бы одно русское слово длины 2+.

    Возвращает ORIGINAL (без изменений), если:
    - текст пустой;
    - длина < NYA_MIN_LEN (слишком короткое сообщение);
    - не содержит русских слов (латиница/цифры/эмодзи-only).

    Параметр ``rng`` — внешний Random (для детерминизма внутри одной сессии).
    Если None — создаётся локальный rng (поведение не-детерминировано между
    вызовами, что добавляет «игру»).

    Гарантия:
        Если текст прошёл guards (длина + русские слова) — результат ВСЕГДА
        отличается от original. Это устраняет проблему "иногда `.ня`
        просто не редактирует" — теперь видимый эффект либо есть всегда,
        либо skip'ается (только для явно не-подходящих текстов).
    """
    if rng is None:
        rng = random.Random()

    # Defensive: если кто-то случайно передал Telethon Message /
    # aiogram Message (а не строку) — извлекаем текст через getattr.
    # Раньше такие вызовы роняли ``AttributeError: 'Message' object
    # has no attribute 'strip'`` (как в love.py:_is_love). Теперь функция
    # устойчива к любому duck-typed объекту с ``text``/``raw_text``.
    if not isinstance(text, str):
        candidate = (
            getattr(text, "raw_text", None)
            or getattr(text, "text", None)
            or getattr(text, "message", None)
        )
        if isinstance(candidate, str):
            text = candidate
        elif candidate is not None:
            text = str(candidate)
        else:
            text = ""

    stripped = text.strip()
    if not stripped or len(stripped) < NYA_MIN_LEN:
        return text

    # Защита: текст содержит хотя бы одно русское слово длины 2+? Иначе skip.
    # Фильтрует чисто латиницу/числа/эмодзи/тэги — крутить зря не надо.
    if not _WORD_RE.search(stripped):
        return text

    original = text

    # 1) per-word unification: первая буква (consonant 2-3 reps, vowel 3 reps).
    #    Track whether ANY word actually changed — для финальной гарантии.
    word_changed = False

    def _rewrite_word(m: re.Match) -> str:
        nonlocal word_changed
        word = m.group(0)
        new = _double_first_char(word, rng)
        if new != word:
            word_changed = True
        return new

    rewritten = _WORD_RE.sub(_rewrite_word, text)

    # 2) *action*-префикс (NYA_ACTION_PROB gate).
    if rng.random() < NYA_ACTION_PROB:
        action = rng.choice(CATGIRL_ACTIONS)
        rewritten = f"{action} {rewritten.lstrip()}"

    # 3) Каомодзи-суффикс (NYA_KAOMOJI_PROB gate).
    if rng.random() < NYA_KAOMOJI_PROB:
        km = rng.choice(CATGIRL_KAOMOJI)
        rewritten = f"{rewritten.rstrip()} {km}"

    # 4) GUARANTEE видимого изменения.
    #    Если ничего не сработало (ни одно слово не было переписано И
    #    ни одно decoration не сработало) — форсируем kaomoji-suffix 100%.
    #    Это устраняет баг "иногда просто обычное сообщение отправляет":
    #    пользователь ожидает ВСЕГДА видеть catgirl-стиль в этом режиме.
    if rewritten.strip() == original.strip():
        km = rng.choice(CATGIRL_KAOMOJI)
        rewritten = f"{rewritten.rstrip()} {km}"

    # 5) Финальный страж (на случай corner-case если trim совпал).
    #    Дополнительная маркер-точка — на случай если даже предыдущий шаг
    #    не изменил результат (теоретически не должно произойти, но защита).
    if rewritten.strip() == original.strip():
        marker = rng.choice(_FINAL_MARKERS)
        rewritten = f"{rewritten.rstrip()} {marker}"

    return rewritten
