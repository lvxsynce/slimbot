from aiogram import Router, types

from config import EXPENSIVE_COMMAND_LIMIT, EXPENSIVE_COMMAND_WINDOW
from utils.calc import calc as do_calc
from utils.escape import esc
from utils.rate_limit import allow
from utils.storage import session_exists
from utils.texts import Texts, render_for_user
from ._base import command_card, thread_kwargs

router = Router()

TR_CMDS = (".tr", ".перевод", ".пер", ".перевести")
CALC_CMDS = (".calc", ".калк")
SAVE_CMDS = (".save", ".сохранить")

# Подкоманда `.tr ai [lang] <text|reply>` — умный AI-перевод через LLM.
# Учитывает сленг, стиль, тон, идиомы. lang опционален (default ru).
TR_AI_KEYWORDS = ("ai", "аи", "ии", "смарт", "smart", "умный")

# ISO-код → название для AI-промпта (только для отображения в запросе к LLM).
_ISO_TO_NAME: dict[str, str] = {
    "ru": "русский", "en": "английский", "de": "немецкий",
    "fr": "французский", "es": "испанский", "it": "итальянский",
    "pt": "португальский", "zh": "китайский", "ja": "японский",
    "ko": "корейский", "ar": "арабский", "tr": "турецкий",
    "pl": "польский", "uk": "украинский", "be": "белорусский",
    "kk": "казахский", "uz": "узбекский",
}
# Русское название → ISO-код (для парсинга пользовательского ввода).
_RU_TO_ISO: dict[str, str] = {v: k for k, v in _ISO_TO_NAME.items()}
_RU_TO_ISO.update({
    "англиский": "en",
    "руский": "ru",
    "немецкий": "de",
})

# Публичный маппинг (для обратной совместимости и экспорта, если нужен).
TR_LANG_NAMES: dict[str, str] = {**_ISO_TO_NAME, **_RU_TO_ISO}


def _normalize_lang(raw: str) -> str:
    """Нормализует язык к ISO-коду.

    Принимает: ISO-код ('en', 'ru'), русское название ('английский').
    Возвращает ISO-код для промпта.
    Неизвестный ввод возвращается как есть и проверяется вызывающим кодом.
    """
    raw = raw.lower().strip(",.;:")
    if raw in _ISO_TO_NAME:
        return raw  # уже ISO
    if raw in _RU_TO_ISO:
        return _RU_TO_ISO[raw]  # русское → ISO
    return raw  # неизвестно — пробрасываем как есть


def _lang_display(iso: str) -> str:
    """ISO-код → читаемое название для AI-промпта. Fallback — сам код."""
    return _ISO_TO_NAME.get(iso, iso)


def _head(text: str) -> str:
    return (text or "").strip().split(maxsplit=1)[0].lower()


def _tr_check(t: str | None) -> bool:
    return bool(t) and _head(t) in TR_CMDS


def _calc_check(t: str | None) -> bool:
    return bool(t) and _head(t) in CALC_CMDS


def _save_check(t: str | None) -> bool:
    return bool(t) and _head(t) in SAVE_CMDS


def _parse_tr_args(args: str) -> tuple[bool, str, str]:
    """Парсит аргументы .tr.

    Возвращает (is_ai, lang_iso, text_to_translate).
    - is_ai=True если первый токен — TR_AI_KEYWORDS.
    - lang_iso: ISO-код (en/ru/de...), пригодный для LLM.
    - text: текст после lang (пустой → нужен reply_text).
    """
    parts = args.split(maxsplit=1) if args else []
    if not parts:
        return False, "ru", ""

    first = parts[0].lower().strip(",.;:")
    rest = parts[1].strip() if len(parts) > 1 else ""

    # .tr ai [lang] text
    if first in TR_AI_KEYWORDS:
        rest_parts = rest.split(maxsplit=1) if rest else []
        if rest_parts:
            candidate = rest_parts[0].lower().strip(",.;:")
            # Проверяем что candidate — язык (ISO или русское название), а не слово текста.
            if candidate in _ISO_TO_NAME or candidate in _RU_TO_ISO:
                lang = _normalize_lang(candidate)
                text = rest_parts[1].strip() if len(rest_parts) > 1 else ""
            else:
                lang = "ru"
                text = rest
        else:
            lang = "ru"
            text = ""
        return True, lang, text

    # Обычный .tr: первый токен — lang (ISO или русское название → нормализуем).
    # Если язык не распознан, считаем всю строку текстом и используем русский.
    if first not in _ISO_TO_NAME and first not in _RU_TO_ISO:
        return False, "ru", args.strip()
    lang = _normalize_lang(first)
    return False, lang, rest


async def _do_tr_ai(uid: str, lang: str, target: str) -> str:
    """AI-перевод через LLM с учётом сленга, стиля и идиом."""
    from utils.ai import ask
    from utils.ai_prompts import TR_AI_SYSTEM, fmt_tr_ai_user

    if not target:
        return command_card(
            "Tr",
            "[?] Использование:\n"
            "<code>.tr ru текст</code> — AI-перевод на русский\n"
            "<code>.tr en текст</code> — AI-перевод на английский\n"
            "или ответом на сообщение: <code>.tr en</code>",
        )

    # Название языка для промпта (ISO → читаемое название).
    lang_name = _lang_display(lang)

    answer, err = await ask(
        fmt_tr_ai_user(lang_name, target),
        system_override=TR_AI_SYSTEM,
    )
    if err:
        from utils.escape import esc
        return command_card("Tr", f"[x] AI-перевод: {esc(err)}")

    from utils.escape import esc
    text = esc(answer.strip())
    if not text:
        return command_card("Tr", "[x] AI вернул пустой ответ.")

    # Исходный язык отдельно не определяем: показываем направление AI-перевода.
    head = f"<b>→ {lang_name}</b> <i>(AI)</i>"
    return command_card("Tr", f"{head}\n{text}")


async def translate_with_ai(uid: str, lang: str, target: str) -> tuple[str, str | None]:
    """Возвращает сырой AI-перевод для обычного и auto-режимов."""
    from utils.ai import ask
    from utils.ai_prompts import TR_AI_SYSTEM, fmt_tr_ai_user
    answer, err = await ask(
        fmt_tr_ai_user(_lang_display(lang), target),
        system_override=TR_AI_SYSTEM,
    )
    return answer.strip(), err


async def _do_tr(uid: str, args: str, reply_text: str | None) -> str:
    is_ai, lang, text_after = _parse_tr_args(args)

    target = text_after or reply_text or ""

    if not target:
        return Texts.Tr.HELP.render(premium=False)
    return await _do_tr_ai(uid, lang, target)


@router.message(lambda m: _tr_check(m.text))
async def cmd_tr_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    args = (message.text or "").strip().split(maxsplit=1)
    args = args[1] if len(args) > 1 else ""
    reply_text = None
    if message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption
    is_ai, _, _ = _parse_tr_args(args)
    if is_ai:
        sent = await message.reply(command_card("Tr", "[…] AI-перевод…"), **thread_kwargs(message))
        result = await _do_tr(uid, args, reply_text)
        await sent.edit_text(result)
        return
    result = await _do_tr(uid, args, reply_text)
    await message.reply(result, **thread_kwargs(message))


async def _do_calc(uid, args: str) -> str:
    if not args:
        return Texts.Calc.HELP.render(premium=False)
    try:
        res = do_calc(args)
    except Exception as e:
        return command_card("Calc", f"[x] {esc(type(e).__name__)}: {esc(str(e))}")
    return command_card(
        "Calc",
        await render_for_user(uid, Texts.Calc.RESULT, expr=args.strip(), res=str(res)),
    )


def _calc_limited(uid: str) -> bool:
    return allow(uid, "network", limit=EXPENSIVE_COMMAND_LIMIT, window=EXPENSIVE_COMMAND_WINDOW)


@router.message(lambda m: _calc_check(m.text))
async def cmd_calc_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    args = (message.text or "").strip().split(maxsplit=1)
    args = args[1] if len(args) > 1 else ""
    if not _calc_limited(uid):
        await message.reply(command_card("Calc", "[x] Слишком много запросов. Подожди немного."), parse_mode="html", **thread_kwargs(message))
        return
    await message.reply(await _do_calc(uid, args), **thread_kwargs(message))


# .save работает только через Telethon (нужен доступ к Saved Messages пользователя).
@router.message(lambda m: _save_check(m.text))
async def cmd_save_private(message: types.Message):
    uid = str(message.from_user.id) if message.from_user else ""
    tkw = thread_kwargs(message)
    if not session_exists(uid):
        await message.reply(command_card("Save", Texts.Save.NEED_SESSION_PRIVATE.render(premium=False)), **tkw)
        return
    if not message.reply_to_message:
        await message.reply(command_card("Save", Texts.Save.NEED_REPLY_SHORT.render(premium=False)), **tkw)
        return
    from utils.telethon_manager import telethon_manager
    client = telethon_manager.get_client(uid)
    if not client:
        await message.reply(command_card("Save", Texts.Save.NO_SESSION.render(premium=False)), **tkw)
        return
    try:
        await client.forward_messages("me", message.reply_to_message.message_id, message.chat.id)
        await message.reply(command_card("Save", Texts.Save.OK.render(premium=False)), **tkw)
    except Exception as e:
        from utils.escape import esc
        await message.reply(command_card("Save", f"[x] {esc(type(e).__name__ + ': ' + str(e))}"), **tkw)
