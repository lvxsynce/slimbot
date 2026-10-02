"""aiogram-слой модулей: приём .py-файлов и UI управления.

Три слоя:

1. **Установка.** Юзер присылает боту `.py`-документ в личку → бот
   сохраняет исходник, проверяет его (`utils.modules.check_install`) и
   показывает результат. Если команда модуля совпала с системной или с
   чужой — модуль ставится, но конфликт уходит в `Conflict`-очередь, и
   юзер выбирает: оставить системную, заменить её, задать своё имя или
   откатить установку.
2. **Управление.** `/modules` — список, вкл/выкл, перезагрузка, удаление,
   список отключённых системных команд.
3. **Диспетчеризация dot-команд в личке.** Суб-роутер, подключённый
   ПЕРЕД `_fallback_router`: иначе fallback отвечал бы подсказкой вместо
   модуля.

Всё это работает и без Telethon-сессии: модуль — обычный пользовательский
код, а не доступ к аккаунту.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aiogram import F, Router, types

from utils import modules as M
from utils import rate_limit_gate as gate
from utils.escape import esc as _esc
from utils.module_api import CommandSpec
from utils.modules import Context, ModuleError
from utils.storage import is_module_enabled

from ._base import command_card, thread_kwargs

logger = logging.getLogger(__name__)

#: Имя нужно `test_docs_drift` и тестам модулей: сопоставление роутеров
#: идёт по имени, а не по идентичности (так же назван `_fallback_router`).
router = Router(name="modules")

#: Сессия ожидания: uid -> {"name", "stage"}. Живёт недолго — это
#: промежуточное состояние диалога установки, а не хранилище.
_pending_install: dict[str, dict] = {}

#: uid -> {"module", "head", "stage": "rename"} — ждём новое имя команды.
_pending_rename: dict[str, dict] = {}

#: Ответ на сообщение в личке: пользовательский текст → карточка.
_TEXT_RE = re.compile(r"^\s*(?P<head>\S+)\s*(?P<args>.*)$", re.DOTALL)


# --------------------------------------------------------------------------
# Карточки
# --------------------------------------------------------------------------


def _card(title: str, body: str) -> str:
    return command_card(title, body)


async def _edit(message: types.Message, text: str, markup=None) -> None:
    """Отредактировать сообщение-установщик, молча игнорируя «not modified»."""
    try:
        await message.edit_text(text, reply_markup=markup, **thread_kwargs(message))
    except Exception:
        pass


# --------------------------------------------------------------------------
# /modules — управление
# --------------------------------------------------------------------------

USAGE = (
    "<b>Модули</b>\n\n"
    "Пришли <code>.py</code>-файл в личку — бот сохранит и подключит его.\n"
    "Формат и пример: <code>modules_dev.md</code>.\n\n"
    "<b>Команды</b>\n"
    "• <code>/modules</code> — этот список\n"
    "• <code>/modules вкл ИМЯ</code> / <code>/modules выкл ИМЯ</code>\n"
    "• <code>/modules перезагрузить [ИМЯ]</code>\n"
    "• <code>/modules удалить ИМЯ</code>\n"
    "• <code>/modules система</code> — список системных команд\n"
    "• <code>/modules отключить .ping</code> — выключить системную команду\n"
    "• <code>/modules вернуть .ping</code> — включить обратно\n\n"
    "<b>Конфликты</b>\n"
    "Если команда модуля совпадает с системной, бот спросит: оставить "
    "системную, заменить её, задать своё имя или откатить модуль."
)


@router.message(F.text == "/modules")
async def cmd_modules(message: types.Message):
    uid = str(message.from_user.id) if message.from_user else ""
    if not M.modules_enabled():
        await message.answer(_card("Модули", "[x] Подсистема выключена (MODULES_ENABLED=0)."),
                             **thread_kwargs(message))
        return
    if not M.module_allowed(uid):
        await message.answer(_card("Модули", "[x] Загрузка модулей недоступна для этого аккаунта."),
                             **thread_kwargs(message))
        return
    await message.answer(_card("Модули", USAGE), **thread_kwargs(message))
    await _send_list(message)


@router.message(F.text.regexp(r"^/modules\s+(вкл|выкл|off|on)\s+(\S+)\s*$"))
async def cmd_modules_toggle(message: types.Message):
    uid = str(message.from_user.id) if message.from_user else ""
    action, name = (message.text or "").split()[-2:]
    if not _can_manage(message):
        return
    if name not in M.installed_names(uid):
        await message.answer(_card("Модули", f"[x] Модуль <code>{_esc(name)}</code> не найден."),
                             **thread_kwargs(message))
        return
    if action in ("выкл", "off"):
        M.unload_module(uid, name)
        from utils.storage import set_module_enabled

        set_module_enabled(uid, name, False)
        await message.answer(_card("Модули", f"[x] <code>{_esc(name)}</code> выключен."),
                             **thread_kwargs(message))
        return
    from utils.storage import set_module_enabled

    set_module_enabled(uid, name, True)
    try:
        info = M.load_module(uid, name)
    except ModuleError as e:
        await message.answer(_card("Модули", f"[x] Не удалось включить: {_esc(str(e))}"),
                             **thread_kwargs(message))
        return
    await message.answer(
        _card("Модули", f"[x] <code>{_esc(name)}</code> включён: "
                        f"команд {len(info.commands)}, inline {len(info.inline)}."),
        **thread_kwargs(message),
    )


@router.message(F.text.regexp(r"^/modules\s+перезагрузить(?:\s+(\S+))?\s*$"))
async def cmd_modules_reload(message: types.Message):
    uid = str(message.from_user.id) if message.from_user else ""
    if not _can_manage(message):
        return
    parts = (message.text or "").split()
    name = parts[2] if len(parts) > 2 else ""
    if name:
        if name not in M.installed_names(uid):
            await message.answer(_card("Модули", f"[x] Модуль <code>{_esc(name)}</code> не найден."),
                                 **thread_kwargs(message))
            return
        try:
            info = M.load_module(uid, name)
        except ModuleError as e:
            await message.answer(_card("Модули", f"[x] {_esc(str(e))}"), **thread_kwargs(message))
            return
        await message.answer(
            _card("Модули", f"[x] <code>{_esc(name)}</code> перезагружен."),
            **thread_kwargs(message),
        )
        return
    errors = M.reload_all()
    mine = errors.get(uid, "")
    body = "[x] Все модули перезагружены." if not mine else f"[x] {_esc(mine)}"
    await message.answer(_card("Модули", body), **thread_kwargs(message))


@router.message(F.text.regexp(r"^/modules\s+удалить\s+(\S+)\s*$"))
async def cmd_modules_delete(message: types.Message):
    uid = str(message.from_user.id) if message.from_user else ""
    if not _can_manage(message):
        return
    name = (message.text or "").split()[-1]
    if not M.delete_module(uid, name):
        await message.answer(_card("Модули", f"[x] Модуль <code>{_esc(name)}</code> не найден."),
                             **thread_kwargs(message))
        return
    await message.answer(_card("Модули", f"[x] Модуль <code>{_esc(name)}</code> удалён."),
                         **thread_kwargs(message))


@router.message(F.text == "/modules система")
async def cmd_modules_system(message: types.Message):
    """Список системных команд и их состояние (включена/выключена)."""
    uid = str(message.from_user.id) if message.from_user else ""
    if not _can_manage(message):
        return
    off = M.disabled_system(uid)
    heads = sorted(M.system_heads())
    lines = []
    for head in heads:
        mark = "[x]" if head in off else "[ ]"
        lines.append(f"{mark} <code>{_esc(head)}</code>")
    body = "\n".join(lines) or "[ ] Системные команды не загружены."
    body += (
        "\n\n<i>[x] = выключена. Включить: /modules вернуть .команда</i>"
    )
    await message.answer(_card("Модули · система", body), **thread_kwargs(message))


@router.message(F.text.regexp(r"^/modules\s+(отключить|вернуть)\s+(\S+)\s*$"))
async def cmd_modules_toggle_system(message: types.Message):
    uid = str(message.from_user.id) if message.from_user else ""
    if not _can_manage(message):
        return
    action, raw = (message.text or "").split()[-2:]
    head = raw if raw.startswith(".") else "." + raw
    if head not in M.system_heads():
        await message.answer(
            _card("Модули", f"[x] <code>{_esc(head)}</code> — не системная команда."),
            **thread_kwargs(message),
        )
        return
    if action == "отключить":
        M.disable_system(uid, head)
        await message.answer(_card("Модули", f"[x] <code>{_esc(head)}</code> отключена."),
                             **thread_kwargs(message))
        return
    M.enable_system(uid, head)
    await message.answer(_card("Модули", f"[x] <code>{_esc(head)}</code> включена."),
                         **thread_kwargs(message))


# --------------------------------------------------------------------------
# Установка: приём .py-файла
# --------------------------------------------------------------------------


@router.message(F.document)
async def on_document(message: types.Message):
    """Юзер прислал файл — пробуем считать его модулем."""
    uid = str(message.from_user.id) if message.from_user else ""
    if not M.modules_enabled() or not M.module_allowed(uid):
        return
    doc = message.document
    name = (getattr(doc, "file_name", "") or "").strip()
    if not name.lower().endswith(".py"):
        return

    slug = name[:-3]
    if not M.NAME_RE.match(slug):
        await message.answer(
            _card("Модули",
                  f"[x] Имя файла <code>{_esc(name)}</code> не подходит: только "
                  "латиница, цифры и <code>_</code> (до 32 символов)."),
            **thread_kwargs(message),
        )
        return

    limit_files = M.max_files()
    count = len(M.list_files(uid))
    if count >= limit_files and slug not in M.list_files(uid):
        await message.answer(
            _card("Модули",
                  f"[x] Лимит: {limit_files} модулей. "
                  "Удали лишний: <code>/modules удалить ИМЯ</code>"),
            **thread_kwargs(message),
        )
        return

    limit_bytes = M.max_bytes()
    if getattr(doc, "file_size", 0) and doc.file_size > limit_bytes:
        await message.answer(
            _card("Модули",
                  f"[x] Файл {doc.file_size // 1024} КБ, максимум "
                  f"{limit_bytes // 1024} КБ."),
            **thread_kwargs(message),
        )
        return

    from aiogram.types import FSInputFile

    try:
        await message.answer(_card("Модули", "[...] Читаю модуль…"), **thread_kwargs(message))
        source = await _download(message, doc)
    except Exception as e:
        logger.exception("modules: download failed uid=%s", uid)
        await message.answer(_card("Модули", f"[x] Не удалось прочитать файл: {_esc(str(e))}"),
                             **thread_kwargs(message))
        return

    await _install(message, uid, slug, source)


async def _download(message: types.Message, doc: types.Document) -> str:
    """Скачать документ в память.

    `FSInputFile` с file_id заставляет aiogram отдать поток, не создавая
    временный файл на дише — модуль ещё не проверен, и класть его в
    DATA_DIR до проверки нельзя.
    """
    import io

    from aiogram import Bot

    bot: Bot = message.bot
    buf = io.BytesIO()
    await bot.download(FSInputFile(doc.file_id, filename=doc.file_name), destination=buf)
    return buf.getvalue().decode("utf-8", errors="replace")


async def _install(message: types.Message, uid: str, name: str, source: str) -> None:
    """Проверить, сохранить, загрузить; при конфликте — спросить."""
    problems = M.check_install(uid, name, source)
    hard = [p for p in problems if not _is_soft_conflict(p)]
    if hard:
        body = "\n".join(f"[x] {_esc(p)}" for p in hard)
        await message.answer(
            _card("Модули", f"{body}\n\n<b>{_esc(name)}.py</b> не установлен."),
            **thread_kwargs(message),
        )
        return

    try:
        M.save_module_source(uid, name, source)
        info = M.load_module(uid, name)
    except ModuleError as e:
        await message.answer(_card("Модули", f"[x] {_esc(str(e))}"), **thread_kwargs(message))
        return

    from utils.storage import register_module

    register_module(uid, name)

    off = M.disabled_system(uid)
    conflicts = [
        M.Conflict(
            uid=uid,
            module=name,
            head=head,
            system_title=M.system_command_title(head),
            module_desc=next((s.desc for s in info.commands if s.head == head), ""),
        )
        for head in {h for s in info.commands for h in (s.head, *s.aliases)}
        if head in M.system_heads() or head in off
    ]
    for c in conflicts:
        M.register_conflict(c)

    body = (
        f"<b>{_esc(name)}.py</b> — версия {_esc(info.version)}, "
        f"автор {_esc(info.author or '?')}\n"
        f"Команд: <code>{len(info.commands)}</code> · "
        f"inline: <code>{len(info.inline)}</code>"
    )
    if conflicts:
        await message.answer(
            _card("Модули", f"{body}\n\n[!] Есть конфликт с системными командами."),
            **thread_kwargs(message),
        )
        for c in conflicts:
            await _ask_conflict(message, c)
        return
    await message.answer(_card("Модули", body), **thread_kwargs(message))


def _is_soft_conflict(problem: str) -> bool:
    """Конфликт (мягкий) или ошибка (жёсткая)?

    Конфликт — это не поломка: модуль устанавливается, решение принимает
    юзер. Ошибка — модуль не ставится вовсе.
    """
    return "системная команда" in problem or "уже занят" in problem \
        or "системный inline" in problem


async def _ask_conflict(message: types.Message, c: M.Conflict) -> None:
    """Карточка выбора по конфликту: оставить / заменить / переименовать / откатить."""
    M.register_conflict(c)
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="Оставить системную", callback_data=f"mcf:keep:{c.key}"),
            types.InlineKeyboardButton(text="Заменить", callback_data=f"mcf:replace:{c.key}"),
        ],
        [
            types.InlineKeyboardButton(text="Своё имя", callback_data=f"mcf:rename:{c.key}"),
            types.InlineKeyboardButton(text="Откатить модуль", callback_data=f"mcf:rollback:{c.key}"),
        ],
    ])
    await message.answer(
        _card(
            "Модули · конфликт",
            f"Команда <code>{_esc(c.head)}</code> уже занята системной "
            f"(<b>{_esc(c.system_title)}</b>).\n"
            f"Модуль: <b>{_esc(c.module)}</b> — {_esc(c.module_desc or 'без описания')}\n\n"
            "[?] <b>Оставить системную</b> — модуль не перехватит команду\n"
            "[x] <b>Заменить</b> — команда уйдёт в модуль\n"
            "[i] <b>Своё имя</b> — спросим новое название команды\n"
            "[<-] <b>Откатить модуль</b> — удалим его целиком",
        ),
        reply_markup=kb,
        **thread_kwargs(message),
    )


@router.callback_query(F.data.startswith("mcf:"))
async def conflict_cb(callback: types.CallbackQuery):
    uid = str(callback.from_user.id) if callback.from_user else ""
    _, action, key = (callback.data or "").split(":", 2)
    c = M.pending_conflict(key)

    if c is None:
        await callback.answer("Решение уже принято или истекло", show_alert=True)
        return
    if c.uid != uid:
        await callback.answer("Это не твой конфликт", show_alert=True)
        return

    if action == "rename":
        _pending_rename[uid] = {"module": c.module, "head": c.head, "conflict": key}
        await _edit(
            callback.message,
            _card("Модули · новое имя",
                   f"Пришли новое имя для команды из модуля "
                   f"<b>{_esc(c.module)}</b>.\n\n"
                   f"Например: <code>{_esc(c.head)}x</code> или <code>.note</code>.\n"
                   "<i>Отмена: /modules</i>"),
        )
        await callback.answer()
        return

    result = M.resolve_conflict(key, action)

    if action == "rollback":
        M.delete_module(uid, c.module)
        text = _card("Модули", f"Модуль <b>{_esc(c.module)}</b> откатан и удалён.")
    elif action == "keep":
        text = _card("Модули", f"Оставлена системная команда <code>{_esc(c.head)}</code>.")
    elif action == "replace":
        text = _card("Модули",
                     f"Системная <code>{_esc(c.head)}</code> отключена, "
                     f"её обрабатывает модуль <b>{_esc(c.module)}</b>.")
    else:
        text = _card("Модули", f"[x] Не удалось: {result}")

    await _edit(callback.message, text)
    await callback.answer()


@router.message(F.text)
async def pending_rename_listener(message: types.Message):
    """Ожидание нового имени команды (после «Своё имя»)."""
    uid = str(message.from_user.id) if message.from_user else ""
    state = _pending_rename.get(uid)
    if not state:
        return
    if (message.text or "").strip().startswith("/modules"):
        _pending_rename.pop(uid, None)
        return

    _pending_rename.pop(uid, None)
    result = M.resolve_conflict(state["conflict"], "rename", message.text or "")
    if result.startswith("renamed:"):
        head = result.split(":", 1)[1]
        ok = _rename_command(uid, state["module"], state["head"], head)
        if ok:
            await message.answer(
                _card("Модули",
                       f"Команда переименована: <code>{_esc(state['head'])}</code> → "
                       f"<code>{_esc(head)}</code>"),
                **thread_kwargs(message),
            )
            return
        text = f"[x] Не удалось переименовать в <code>{_esc(head)}</code>."
    elif result == "taken":
        text = f"[x] <code>{_esc(message.text or '')}</code> уже занято."
    else:
        text = f"[x] Некорректное имя: {_esc(message.text or '')}"
    await message.answer(_card("Модули", text), **thread_kwargs(message))


def _rename_command(uid: str, module: str, old_head: str, new_head: str) -> bool:
    """Переименовать команду модуля в памяти и перезагрузить модуль.

    Правка исходника не делается: после перезагрузки модуль снова
    объявит старый head, и он снова станет конфликтным. Поэтому
    переименование живёт только в реестре (`_overrides`), а модуль
    грузится поверх.
    """
    M.override_head(uid, module, old_head, new_head)
    try:
        M.load_module(uid, module)
    except ModuleError as e:
        logger.warning("modules: rename reload failed %s: %s", module, e)
        return False
    return True


# --------------------------------------------------------------------------
# Список модулей
# --------------------------------------------------------------------------


async def _send_list(message: types.Message) -> None:
    uid = str(message.from_user.id) if message.from_user else ""
    names = M.installed_names(uid)
    if not names:
        await message.answer(
            _card("Модули · список", "[ ] Пока пусто. Пришли <code>.py</code>-файл в личку."),
            **thread_kwargs(message),
        )
        return
    lines = []
    for name in names:
        on = is_module_enabled(uid, name)
        info = M.list_modules(uid).get(name)
        loaded = "[x]" if info and not info.error else ("[ ]" if not info else "[!]")
        detail = ""
        if info and not info.error:
            detail = f" · команд {len(info.commands)}, inline {len(info.inline)}"
        elif info and info.error:
            detail = f" · ошибка: {_esc(info.error)}"
        lines.append(f"{loaded} <b>{_esc(name)}</b> — {'включён' if on else 'выключен'}{detail}")
    off = sorted(M.disabled_system(uid))
    body = "\n".join(lines)
    if off:
        body += "\n\n<b>Отключённые системные</b>\n" + "\n".join(
            f"[x] <code>{_esc(h)}</code>" for h in off
        )
    await message.answer(_card("Модули · список", body), **thread_kwargs(message))


@router.callback_query(F.data == "modules:list")
async def list_cb(callback: types.CallbackQuery):
    uid = str(callback.from_user.id) if callback.from_user else ""
    if not _can_manage(callback):
        return
    await _send_list(callback.message)
    await callback.answer()


# --------------------------------------------------------------------------
# Диспетчеризация dot-команд модулей в личке
# --------------------------------------------------------------------------


@router.message(F.text.regexp(r"^\.[^\s]+"))
async def dispatch_module_command(message: types.Message):
    """dot-команда модуля в личке с ботом.

    Стоит в цепочке ДО `_fallback_router`: иначе тот отвечал бы подсказкой
    вместо модуля (и подсказка была бы неверной — команда существует).

    Изоляция: `find_command` возвращает спек только для того uid, который
    установил модуль, поэтому чужие команды сюда не попадают.

    Правила совпадают с Telethon-путем (`_handle_modules`):
      * head не заявлен модулем → не наше (системная цепочка / fallback);
      * head заявлен, но юзер НЕ выбрал «заменить» для системной команды →
        модуль молчит, иначе он перехватил бы `.ping` молча;
      * head заявлен и системная команда отключена юзером → выполняем.
    """
    uid = str(message.from_user.id) if message.from_user else ""
    if not M.modules_enabled() or not uid or not M.module_allowed(uid):
        return
    text = (message.text or "").strip()
    head = text.split(maxsplit=1)[0].lower()
    spec = M.find_command(uid, head)
    if spec is None:
        return
    if "bot" not in getattr(spec.handler, "_module_paths", ("telethon", "bot")):
        return
    # Модуль заявил системную команду, но юзер не подтвердил замену —
    # отдаём обработку дальше (fallback предложит подсказку).
    if head in M.system_heads() and not M.is_system_disabled(uid, head):
        return

    match = _TEXT_RE.match(text)
    args = (match.group("args") or "").strip() if match else ""
    # Гейт лимитов — тот же, что у встроенных команд: бюджет модуля
    # прописан в `rate_limit_gate.BUDGETS` при загрузке (`rate=`).
    if gate.is_limited(head) and not gate.check(head, uid):
        await message.reply(gate.RATE_LIMIT_TEXT, **thread_kwargs(message))
        return

    ctx = _make_ctx(uid, head, args, message, spec, bot=getattr(message, "bot", None))
    result = await M.run_command(uid, spec, ctx)
    if result is None:
        return
    text_out = result if isinstance(result, str) else str(result)
    if not text_out.strip():
        return
    try:
        await message.reply(text_out, **thread_kwargs(message))
    except Exception:
        logger.exception("modules: reply failed for %s", head)


def _make_ctx(
    uid: str,
    head: str,
    args: str,
    event: Any,
    spec: CommandSpec,
    *,
    bot: Any = None,
    thread_id: int = 0,
) -> Context:
    """Собрать `Context` для хендлера модуля."""
    from utils.module_state import ModuleState

    info = M.list_modules(uid).get(_module_of(uid, spec), None)
    return Context(
        user_id=uid,
        args=args,
        argv=args.split(),
        head=head,
        event=event,
        client=bot,
        module=info,
        meta={
            "name": info.name if info else "",
            "title": info.title if info else "",
            "version": info.version if info else "",
            "author": info.author if info else "",
            "description": info.description if info else "",
        },
        state=ModuleState(uid, info.name if info else ""),
        thread_id=thread_id,
    )


def _module_of(uid: str, spec: CommandSpec) -> str:
    for name, info in M.list_modules(uid).items():
        if any(s is spec for s in info.commands):
            return name
    return ""


def _can_manage(event: Any) -> bool:
    """Гейт UI: подсистема включена и юзеру разрешено."""
    uid = str(event.from_user.id) if event.from_user else ""
    return M.modules_enabled() and bool(uid) and M.module_allowed(uid)