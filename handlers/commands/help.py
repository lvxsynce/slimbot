from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from ._base import command_card, format_help, render_help, thread_kwargs
from ._base import _help_base_lines, _help_session_lines
from utils.inline_kb import with_inline_hint
from utils.storage import session_exists

router = Router()

HELP_SECTIONS = ("base", "session", "ai")
SECTION_TITLES = {"base": "База", "session": "Сессия", "ai": "ИИ"}


def _section_lines(key: str) -> list[str]:
    """Строки одного раздела справки. Pure — покрыто тестами."""
    if key == "base":
        return _help_base_lines()
    if key == "session":
        return _help_session_lines()
    if key == "ai":
        return [
            "• <code>.ии</code> &lt;запрос&gt; — запрос к ИИ (память диалога)",
            "• <code>.ии</code> (reply) — ответ про сообщение / фото",
            "• <code>.ии сброс</code> — очистить историю",
            "• <code>.ии дебаг</code> — запрос + список тулов",
            "• <code>.ии ctx=N</code> — N сообщений контекста (в чатах)",
        ]
    return []


def _render_section(key: str) -> str:
    title = SECTION_TITLES.get(key, "?")
    body = "<blockquote expandable>\n" + "\n".join(_section_lines(key)) + "\n</blockquote>"
    return command_card(f"Help · {title}", body)


def help_keyboard(has_session: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура /help. Последняя строка — inline-подсказка (`utils.inline_kb`)."""
    section_row = [
        InlineKeyboardButton(text=t, callback_data=f"helpsec:{k}")
        for k, t in (("base", "База"), ("session", "Сессия"), ("ai", "ИИ"))
    ]
    if has_session:
        return with_inline_hint(InlineKeyboardMarkup(inline_keyboard=[
            section_row,
            [InlineKeyboardButton(text="[?] Зачем это", callback_data="w1")],
        ]))
    return with_inline_hint(InlineKeyboardMarkup(inline_keyboard=[
        section_row,
        [InlineKeyboardButton(text="[+] Включить", callback_data="c1")],
        [InlineKeyboardButton(text="[?] Зачем это", callback_data="w1")],
    ]))


@router.callback_query(F.data.startswith("helpsec:"))
async def helpsec_cb(callback: types.CallbackQuery):
    from utils.premium import resolve_effective_uid
    key = (callback.data or "").split(":", 1)[1] if ":" in (callback.data or "") else ""
    uid = await resolve_effective_uid(callback.message)
    has_ss = callback.from_user and session_exists(str(callback.from_user.id))
    if key == "all" or key not in HELP_SECTIONS:
        text = command_card("Help", await render_help(uid, has_ss))
        kb = help_keyboard(has_ss)
    else:
        text = _render_section(key)
        kb = with_inline_hint(InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="[<-] Всё", callback_data="helpsec:all")],
        ]))
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


def _check(text: str | None) -> bool:
    return text and text.strip().lower() in (".help", ".помощь")


@router.message(Command("help", "capabilities"))
async def cmd_help(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    has_ss = message.from_user and session_exists(str(message.from_user.id))
    text = command_card("Help", await render_help(uid, has_ss))
    await message.answer(text, reply_markup=help_keyboard(has_ss), **thread_kwargs(message))


@router.message(lambda msg: _check(msg.text))
async def cmd_dot_help_private(message: types.Message):
    from utils.premium import resolve_effective_uid
    uid = await resolve_effective_uid(message)
    has_ss = message.from_user and session_exists(str(message.from_user.id))
    text = command_card("Help", await render_help(uid, has_ss))
    await message.reply(text, reply_markup=help_keyboard(has_ss), **thread_kwargs(message))
