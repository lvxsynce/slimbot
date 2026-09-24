"""Анимированные ASCII-картинки: ``.love`` и ``.govno``.

Оба — 3 цикла по 7 pulse-эмодзи (шаг 0.45с). После анимации
исходное сообщение с командой удаляется (best-effort; в private-чате
Bot API не разрешает удалять user-message — это silently ignored).
"""

import asyncio
import logging

from aiogram import Router, types

from ._base import thread_kwargs
from utils.storage import was_processed
from utils.texts import Texts, render_for_user
from utils.premium import resolve_effective_uid

logger = logging.getLogger(__name__)

router = Router()


# ===== .love / .любовь =====

LOVE_ROWS = [
    "🤍🤍🤍🤍🤍🤍🤍🤍🤍",
    "🤍🤍❤️❤️🤍❤️❤️🤍🤍",
    "🤍❤️❤️❤️❤️❤️❤️❤️🤍",
    "🤍❤️❤️❤️❤️❤️❤️❤️🤍",
    "🤍🤍❤️❤️❤️❤️❤️🤍🤍",
    "🤍🤍🤍❤️❤️❤️🤍🤍🤍",
    "🤍🤍🤍🤍❤️🤍🤍🤍🤍",
    "🤍🤍🤍🤍🤍🤍🤍🤍🤍",
]
LOVE_PULSE = ["🧡", "💛", "💚", "💙", "💜", "🩷", "❤️"]
LOVE_TRIGGERS = (".love", ".любовь")
LOVE_MARKER = "❤️"


# ===== .govno / .говно =====

# Анфас: 8 строк × 9 символов. 🤢 — «тошнотный фон», 💩 — fill.
# Композиция полностью симметрична .love (тоже 8 строк, fill в сердцевине),
# но с другой палитрой.
GOVNO_ROWS = [
    "🤢🤢🤢🤢🤢🤢🤢🤢🤢",
    "🤢🤢💩💩🤢💩💩🤢🤢",
    "🤢💩💩💩💩💩💩💩🤢",
    "🤢💩💩💩💩💩💩💩🤢",
    "🤢🤢💩💩💩💩💩🤢🤢",
    "🤢🤢🤢💩💩💩🤢🤢🤢",
    "🤢🤢🤢🤢💩🤢🤢🤢🤢",
    "🤢🤢🤢🤢🤢🤢🤢🤢🤢",
]
# Pulse: stink/коричневые оттенки. Финальный кадр — 💩.
GOVNO_PULSE = ["🟤", "🤎", "🦨", "🤢", "🤮", "💩", "🟫"]
GOVNO_TRIGGERS = (".govno", ".говно")
GOVNO_MARKER = "💩"


def make_frame(rows: list[str], marker: str, variant: str) -> str:
    """Склеивает ``rows`` в один кадр, заменяя ``marker`` на ``variant``.

    ``marker`` — плейсхолдер внутри захардкоженных ROWS (мы контролируем
    содержимое, поэтому знаем, что ``marker`` встречается ровно там, где
    должно меняться).
    """
    return "\n".join(row.replace(marker, variant) for row in rows)


async def _run_anim(
    msg: types.Message,
    *,
    rows: list[str],
    marker: str,
    pulse: list[str],
    final_text_obj,
    final_uid: str | int | None,
    delete_origin=None,
) -> None:
    """Прокручивает кадры ``pulse`` × 3 цикла + финал + best-effort delete.

    Args:
        msg: aiogram Message с уже отправленным первым кадром (его edit'им).
        rows / marker / pulse: данные для ``make_frame`` (как у любой
            из двух анимаций).
        final_text_obj: ``Text`` для финала (Texts.Love.LOVE_YOU / GOVNO_DONE).
        final_uid: UID для premium-aware рендера финала. None → fallback.
        delete_origin: optional async callable() -> None. Вызывается в
            ``finally`` — лучшее усилие удалить исходную команду. Ошибки
            глотаются (UX команды должен работать даже если у бота нет
            прав удалять user-message).
    """
    rendered_final = await render_for_user(final_uid, final_text_obj)
    try:
        for _ in range(3):
            for variant in pulse:
                try:
                    await msg.edit_text(make_frame(rows, marker, variant))
                except Exception:
                    # MessageNotModified / flood — продолжаем кадры,
                    # не прерываем анимацию.
                    pass
                await asyncio.sleep(0.45)
        try:
            await msg.edit_text(rendered_final, parse_mode="html")
        except Exception:
            pass
    finally:
        if delete_origin is not None:
            try:
                await delete_origin()
            except Exception as e:
                logger.debug("love/govno delete origin failed: %s", e)


def _is_love(message: types.Message | None) -> bool:
    """aiogram filter: вернуть True если message содержит .love / .любовь.

    aiogram передаёт СЮДА ``types.Message``, а не строку — поэтому
    код defensive: text → caption → ''.
    """
    if message is None:
        return False
    text = (
        getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    )
    return text.strip().lower() in LOVE_TRIGGERS


def _is_govno(message: types.Message | None) -> bool:
    if message is None:
        return False
    text = (
        getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    )
    return text.strip().lower() in GOVNO_TRIGGERS


async def _safe_delete_message(message: types.Message) -> None:
    """Best-effort delete юзер-message. Молча глотает permission errors.

    В private-чате Bot API не разрешает удалять чужие сообщения — сообщение
    останется как есть. В группах/форумах, где бот admin — попробуем удалить
    (если права есть).
    """
    try:
        await message.delete()
    except Exception as e:
        logger.debug("love/govno aiogram delete failed: %s", e)


async def _do_anim(
    message: types.Message,
    *,
    rows: list[str],
    marker: str,
    pulse: list[str],
    final_text_obj,
) -> None:
    """Единая точка для aiogram private. Шлёт анимацию + удаляет команду."""
    if was_processed(
        message.chat.id,
        message.message_id,
        getattr(message, "message_thread_id", None),
    ):
        return
    # Первое сообщение бота — уже полный кадр (marker совпадает с variant),
    # дальше только редактируем его.
    msg = await message.answer(
        make_frame(rows, marker, marker), **thread_kwargs(message)
    )
    uid = await resolve_effective_uid(message)
    await _run_anim(
        msg,
        rows=rows,
        marker=marker,
        pulse=pulse,
        final_text_obj=final_text_obj,
        final_uid=uid,
        delete_origin=lambda: _safe_delete_message(message),
    )


@router.message(_is_love)
async def cmd_love_private(message: types.Message):
    await _do_anim(
        message,
        rows=LOVE_ROWS,
        marker=LOVE_MARKER,
        pulse=LOVE_PULSE,
        final_text_obj=Texts.Love.LOVE_YOU,
    )


@router.message(_is_govno)
async def cmd_govno_private(message: types.Message):
    await _do_anim(
        message,
        rows=GOVNO_ROWS,
        marker=GOVNO_MARKER,
        pulse=GOVNO_PULSE,
        final_text_obj=Texts.Love.GOVNO_DONE,
    )
