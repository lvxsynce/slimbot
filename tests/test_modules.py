"""Пользовательские модули: реестр, загрузка, конфликты, изоляция.

Что тут ломается по-настоящему:

1. **Двойной ответ.** Модуль может зарегистрировать свой aiogram-роутер
   или dot-команду; оба пути обязаны сработать ровно один раз.
2. **Утечка между юзерами.** `_index` keyed по head — если забыть uid,
   команда модуля одного юзера выполнится у другого. Это главный риск
   системы, поэтому проверяется отдельным тестом.
3. **Конфликт с системной командой.** Модуль с `.ping` обязан НЕ
   перехватывать `.ping`, пока юзер явно не выбрал «заменить».
4. **Битый модуль.** Ошибка импорта/таймаут не роняют бота и не молчат.
5. **Отключение системных команд.** Выключенная команда молчит, а не
   отвечает подсказкой «возможно, это .ping?».
"""

import asyncio
import textwrap
from types import SimpleNamespace

import pytest

from utils import cmds, modules as M
from utils.modules import Conflict, Context, ModuleError

UID_A = 700001
UID_B = 700002

#: Модуль с одной dot-командой и одним inline-хендлером.
SIMPLE = textwrap.dedent('''
    from slimbot_api import command, inline

    MODULE = {
        "name": "Заметки",
        "version": "1.0",
        "author": "tester",
        "description": "Тестовый модуль",
    }

    @command(".note", aliases=(".заметка",), desc="Заметка")
    async def note(ctx):
        return f"note:{ctx.args}"

    @inline("note", desc="Заметка inline")
    async def note_inline(ctx):
        return None
''')

#: Модуль, который забирает системную команду `.ping`.
HIJACK = textwrap.dedent('''
    from slimbot_api import command

    MODULE = {"name": "Hijack", "version": "1"}

    @command(".ping", desc="Свой пинг")
    async def myping(ctx):
        return "hijacked"
''')

#: Модуль, который падает при вызове.
BOOM = textwrap.dedent('''
    from slimbot_api import command

    MODULE = {"name": "Boom", "version": "1"}

    @command(".boom")
    async def boom(ctx):
        raise RuntimeError("бум")
''')

#: Модуль, который висит дольше таймаута.
HANG = textwrap.dedent('''
    import asyncio
    from slimbot_api import command

    MODULE = {"name": "Hang", "version": "1"}

    @command(".hang")
    async def hang(ctx):
        await asyncio.sleep(999)
''')

#: Модуль без MODULE = {...}.
NO_META = textwrap.dedent('''
    from slimbot_api import command

    @command(".x")
    async def x(ctx):
        return "x"
''')

#: Модуль, который ничего не регистрирует.
EMPTY = textwrap.dedent('''
    MODULE = {"name": "Пусто", "version": "1"}
''')

#: Синтаксическая ошибка.
BROKEN = "MODULE = {}\ndef oops(:"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Изолировать хранилище модулей в tmp_path.

    Без этого тесты писали бы .py и modules.json в рабочий DATA_DIR:
    модуль, установленный одним тестом, подхватывался бы следующим
    (и переживал бы перезапуск бота — то есть попадал бы в прод).
    """
    import config
    from utils import storage

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "MODULES_DIR", tmp_path / "modules")
    monkeypatch.setattr(storage, "MODULES_FILE", tmp_path / "modules.json")
    storage.module_settings = {}
    yield
    storage.module_settings = {}


@pytest.fixture(autouse=True)
def clean_registry():
    """Каждый тест стартует с пустым реестром и с системными head'ами."""
    M.reset_all()
    M.set_system_heads(cmds.CMDS)
    yield
    M.reset_all()


def _install(uid: int, name: str, source: str):
    M.save_module_source(uid, name, source)
    return M.load_module(uid, name)


# --------------------------------------------------------------------------
# Загрузка
# --------------------------------------------------------------------------

def test_load_registers_command_and_inline():
    info = _install(UID_A, "notes", SIMPLE)
    assert info.name == "notes"
    assert info.title == "Заметки"
    assert info.version == "1.0"
    assert info.author == "tester"
    assert [s.head for s in info.commands] == [".note"]
    assert [s.key for s in info.inline] == ["note"]
    assert M.find_command(UID_A, ".note") is not None
    assert M.find_command(UID_A, ".заметка") is not None
    assert M.find_inline(UID_A, "note") is not None


def test_load_without_meta_is_rejected():
    M.save_module_source(UID_A, "nometa", NO_META)
    with pytest.raises(ModuleError, match="MODULE"):
        M.load_module(UID_A, "nometa")


def test_module_registering_nothing_is_rejected():
    M.save_module_source(UID_A, "empty", EMPTY)
    with pytest.raises(ModuleError, match="ничего не зарегистрировал"):
        M.load_module(UID_A, "empty")


def test_broken_syntax_does_not_raise_from_loader():
    """Ошибка импорта → ModuleError, а не SyntaxError наружу."""
    M.save_module_source(UID_A, "broken", BROKEN)
    with pytest.raises(ModuleError, match="Синтаксис"):
        M.load_module(UID_A, "broken")


def test_failed_import_leaves_no_trace_in_sys_modules():
    """Модуль, упавший на импорте, не должен остаться в sys.modules.

    Иначе следующая попытка загрузки получит полу-инициализированный
    объект из кеша — и ошибка станет другой, чем на самом деле.
    """
    import sys

    src = textwrap.dedent('''
        raise RuntimeError("падаю на импорте")

        from slimbot_api import command

        MODULE = {"name": "Die", "version": "1"}
    ''')
    M.save_module_source(UID_A, "die", src)
    with pytest.raises(ModuleError, match="падаю на импорте"):
        M.load_module(UID_A, "die")
    leaked = [k for k in sys.modules
              if k.startswith(M.MODULE_PREFIX) and "die" in k]
    assert not leaked, leaked


def test_unload_purges_sys_modules():
    import sys

    _install(UID_A, "notes", SIMPLE)
    assert any(k.startswith(M.MODULE_PREFIX) for k in sys.modules)
    M.unload_module(UID_A, "notes")
    assert not [k for k in sys.modules
                if k.startswith(M.MODULE_PREFIX) and "notes" in k]


def test_missing_file_raises():
    with pytest.raises(ModuleError, match="не найден"):
        M.load_module(UID_A, "nope")


def test_bad_name_is_rejected():
    with pytest.raises(ModuleError, match="Имя модуля"):
        M.save_module_source(UID_A, "../escape", SIMPLE)


def test_oversized_source_is_rejected():
    import config

    with pytest.raises(ModuleError, match="максимум"):
        M.save_module_source(UID_A, "big", "x" * (config.MODULES_MAX_BYTES + 10))


def test_reload_picks_up_new_source():
    """Главный сюрприз importlib: без purge отдаётся старый .py."""
    _install(UID_A, "notes", SIMPLE)
    changed = SIMPLE.replace('return f"note:{ctx.args}"', 'return "v2"')
    M.save_module_source(UID_A, "notes", changed)
    info = M.load_module(UID_A, "notes")
    spec = info.commands[0]
    ctx = Context(user_id=str(UID_A))
    assert asyncio.run(M.run_command(str(UID_A), spec, ctx)) == "v2"


def test_unload_removes_commands():
    _install(UID_A, "notes", SIMPLE)
    assert M.unload_module(UID_A, "notes") is True
    assert M.find_command(UID_A, ".note") is None
    assert M.unload_module(UID_A, "notes") is False


def test_delete_removes_file_and_command():
    _install(UID_A, "notes", SIMPLE)
    assert M.delete_module(str(UID_A), "notes") is True
    assert M.find_command(UID_A, ".note") is None
    assert "notes" not in M.list_files(str(UID_A))


# --------------------------------------------------------------------------
# Изоляция между юзерами
# --------------------------------------------------------------------------

def test_command_of_one_user_is_invisible_to_another():
    """ГЛАВНЫЙ тест изоляции: индекс keyed по head, забыть uid = дыра."""
    _install(UID_A, "notes", SIMPLE)
    assert M.find_command(UID_A, ".note") is not None
    assert M.find_command(UID_B, ".note") is None, "модуль утек к другому юзеру"
    assert M.find_inline(UID_B, "note") is None


def test_two_users_can_have_different_modules():
    _install(UID_A, "notes", SIMPLE)
    _install(UID_B, "other", SIMPLE.replace('.note', '.other'))
    assert M.find_command(UID_A, ".other") is None
    assert M.find_command(UID_B, ".note") is None
    assert M.find_command(UID_B, ".other") is not None


def test_two_users_can_have_same_command_name():
    """Одинаковое имя команды у двух юзеров — НЕ конфликт.

    Реестр индексов ключуется по (head, uid), а не только по head:
    плоский индекс отдавал бы обоим спеку первого установленного.
    """
    src_a = SIMPLE.replace('return f"note:{ctx.args}"', 'return "A"')
    src_b = SIMPLE.replace('return f"note:{ctx.args}"', 'return "B"')
    _install(UID_A, "notes_a", src_a)
    _install(UID_B, "notes_b", src_b)

    spec_a, spec_b = M.find_command(UID_A, ".note"), M.find_command(UID_B, ".note")
    assert spec_a is not None and spec_b is not None
    assert spec_a is not spec_b, "оба юзера получили один и тот же спек"

    ctx_a = Context(user_id=str(UID_A))
    ctx_b = Context(user_id=str(UID_B))
    assert asyncio.run(M.run_command(str(UID_A), spec_a, ctx_a)) == "A"
    assert asyncio.run(M.run_command(str(UID_B), spec_b, ctx_b)) == "B"


def test_same_command_name_is_not_reported_as_conflict():
    """`check_install` не должен ругаться на имя, занятое у ДРУГОГО юзера."""
    assert M.check_install(str(UID_B), "notes", SIMPLE) == []


def test_command_heads_scoped_to_user():
    _install(UID_A, "notes", SIMPLE)
    assert ".note" in M.command_heads(str(UID_A))
    assert ".note" not in M.command_heads(str(UID_B))


# --------------------------------------------------------------------------
# Конфликты с системными командами
# --------------------------------------------------------------------------

def test_check_install_flags_system_command():
    problems = M.check_install(str(UID_A), "hijack", HIJACK)
    assert any("системная команда" in p for p in problems)


def test_check_install_flags_system_inline():
    src = SIMPLE.replace('@inline("note"', '@inline("статус"')
    problems = M.check_install(str(UID_A), "x", src)
    assert any("системный inline" in p for p in problems)


def test_check_install_flags_syntax_error():
    assert M.check_install(str(UID_A), "b", BROKEN)[0].startswith("Синтаксис")


def test_check_install_flags_module_without_commands():
    assert M.check_install(str(UID_A), "e", EMPTY)


def test_module_does_not_silently_take_system_command():
    """Установка модуля ≠ замена системной команды.

    Спек в индексе есть (модуль заявил `.ping`), но флаг отключения
    пуст — значит вызов обязан уйти в системную цепочку. Реальный
    проход проверяет `test_hijack_needs_explicit_confirmation`.
    """
    _install(UID_A, "hijack", HIJACK)
    assert M.find_command(UID_A, ".ping") is not None
    assert not M.is_system_disabled(UID_A, ".ping"), "установка != замена"


def test_keep_leaves_system_enabled():
    c = Conflict(uid=str(UID_A), module="hijack", head=".ping",
                 system_title="ping", module_desc="Свой пинг")
    M.register_conflict(c)
    assert M.resolve_conflict(c.key, "keep") == "kept"
    assert not M.is_system_disabled(str(UID_A), ".ping")


def test_replace_disables_system_command():
    c = Conflict(uid=str(UID_A), module="hijack", head=".ping",
                 system_title="ping", module_desc="Свой пинг")
    M.register_conflict(c)
    assert M.resolve_conflict(c.key, "replace") == "replaced"
    assert M.is_system_disabled(str(UID_A), ".ping")


def test_rollback_result():
    c = Conflict(uid=str(UID_A), module="hijack", head=".ping",
                 system_title="ping", module_desc="")
    M.register_conflict(c)
    assert M.resolve_conflict(c.key, "rollback") == "rolled_back"


def test_conflict_key_is_stable_and_callback_safe():
    """callback_data ограничен 64 байтами и [A-Za-z0-9_:]."""
    c = Conflict(uid=str(UID_A), module="x" * 40, head="." + "y" * 40,
                 system_title="ping", module_desc="")
    M.register_conflict(c)
    assert len(c.key) <= 16
    assert c.key.isalnum()
    assert M.pending_conflict(c.key) is c


def test_resolve_unknown_key_is_expired():
    assert M.resolve_conflict("nope", "keep") == "expired"


def test_resolve_rename_normalizes_and_rejects():
    c = Conflict(uid=str(UID_A), module="hijack", head=".ping",
                 system_title="ping", module_desc="")
    M.register_conflict(c)
    assert M.resolve_conflict(c.key, "rename", "myPing") == "renamed:.myping"
    c2 = Conflict(uid=str(UID_A), module="h2", head=".ping",
                  system_title="ping", module_desc="")
    M.register_conflict(c2)
    assert M.resolve_conflict(c2.key, "rename", "with space") == "bad_name"
    c3 = Conflict(uid=str(UID_A), module="h3", head=".ping",
                  system_title="ping", module_desc="")
    M.register_conflict(c3)
    assert M.resolve_conflict(c3.key, "rename", ".time") == "taken"


def test_rename_reroutes_command():
    """После переименования старый head свободен, новый работает."""
    _install(UID_A, "hijack", HIJACK)
    M.override_head(str(UID_A), "hijack", ".ping", ".myping")
    assert M.find_command(UID_A, ".myping") is not None
    assert M.find_command(UID_A, ".ping") is None


def test_rename_survives_reload():
    """Переименование живёт в реестре, а не в исходнике — переживает reload."""
    _install(UID_A, "hijack", HIJACK)
    M.override_head(str(UID_A), "hijack", ".ping", ".myping")
    M.load_module(UID_A, "hijack")
    assert M.find_command(UID_A, ".myping") is not None
    assert M.find_command(UID_A, ".ping") is None


def test_override_rejects_system_name():
    _install(UID_A, "hijack", HIJACK)
    with pytest.raises(ModuleError, match="системная"):
        M.override_head(str(UID_A), "hijack", ".ping", ".time")


def test_override_rejects_taken_name():
    """Имя алиаса тоже занято: нельзя переименовать в уже существующий алиас."""
    _install(UID_A, "notes", SIMPLE)
    with pytest.raises(ModuleError, match="занят"):
        M.override_head(str(UID_A), "notes", ".note", ".заметка")


def test_override_allows_name_used_only_by_another_user():
    """Чужое имя команды — не помеха: у каждого юзера свой реестр."""
    _install(UID_B, "notes", SIMPLE)
    _install(UID_A, "hijack", HIJACK)
    M.override_head(str(UID_A), "hijack", ".ping", ".note")  # не бросает
    assert M.find_command(UID_A, ".note") is not None
    assert M.find_command(UID_B, ".note") is not None


# --------------------------------------------------------------------------
# Отключение системных команд
# --------------------------------------------------------------------------

def test_disable_and_enable_system():
    M.disable_system(UID_A, ".ping")
    assert M.is_system_disabled(str(UID_A), ".ping")
    assert ".ping" in M.disabled_system(str(UID_A))
    M.enable_system(UID_A, ".ping")
    assert not M.is_system_disabled(str(UID_A), ".ping")


def test_disabled_system_is_per_user():
    M.disable_system(UID_A, ".ping")
    assert not M.is_system_disabled(str(UID_B), ".ping")


# --------------------------------------------------------------------------
# Вызов хендлера: ошибки и таймаут не роняют бота
# --------------------------------------------------------------------------

def test_run_command_returns_error_card_on_exception():
    _install(UID_A, "boom", BOOM)
    spec = M.find_command(UID_A, ".boom")
    out = asyncio.run(M.run_command(str(UID_A), spec, Context(user_id=str(UID_A))))
    assert "RuntimeError" in out and "бум" in out


def test_run_command_times_out(monkeypatch):
    import config

    monkeypatch.setattr(config, "MODULES_COMMAND_TIMEOUT", 0.05)
    _install(UID_A, "hang", HANG)
    spec = M.find_command(UID_A, ".hang")
    out = asyncio.run(M.run_command(str(UID_A), spec, Context(user_id=str(UID_A))))
    assert "не ответил" in out


INLINE_HANG = textwrap.dedent('''
    import asyncio
    from slimbot_api import inline

    MODULE = {"name": "HangInline", "version": "1"}

    @inline("hang")
    async def hang(ctx):
        await asyncio.sleep(999)
''')

INLINE_BOOM = textwrap.dedent('''
    from slimbot_api import inline

    MODULE = {"name": "BoomInline", "version": "1"}

    @inline("boom")
    async def boom(ctx):
        raise RuntimeError("бум")
''')


def test_run_inline_timeout_returns_none(monkeypatch):
    import config

    monkeypatch.setattr(config, "MODULES_COMMAND_TIMEOUT", 0.05)
    _install(UID_A, "hanginl", INLINE_HANG)
    spec = M.find_inline(UID_A, "hang")
    assert spec is not None
    assert asyncio.run(
        M.run_inline(str(UID_A), spec, Context(user_id=str(UID_A)))
    ) is None


def test_run_inline_error_returns_none():
    """Падение inline-хендлера не должно оставлять запрос без ответа."""
    _install(UID_A, "boominl", INLINE_BOOM)
    spec = M.find_inline(UID_A, "boom")
    assert spec is not None
    assert asyncio.run(
        M.run_inline(str(UID_A), spec, Context(user_id=str(UID_A)))
    ) is None


# --------------------------------------------------------------------------
# Гейт доступа
# --------------------------------------------------------------------------

def test_module_allowed_respects_allowlist(monkeypatch):
    import config

    monkeypatch.setattr(config, "MODULE_ALLOWLIST", set())
    assert M.module_allowed("999") is True
    assert M.allowlist_active() is False

    monkeypatch.setattr(config, "MODULE_ALLOWLIST", {"111"})
    assert M.module_allowed("111") is True
    assert M.module_allowed("222") is False
    assert M.allowlist_active() is True


def test_denied_user_cannot_run_module(monkeypatch):
    import config

    _install(UID_B, "notes", SIMPLE)
    monkeypatch.setattr(config, "MODULE_ALLOWLIST", {str(UID_A)})
    spec = M.find_command(UID_B, ".note")
    with pytest.raises(ModuleError, match="MODULE_ALLOWLIST"):
        asyncio.run(M.run_command(str(UID_B), spec, Context(user_id=str(UID_B))))


# --------------------------------------------------------------------------
# Интеграция: Telethon-путь
# --------------------------------------------------------------------------


class FakeEvent:
    """Минимальный Telethon `NewMessage(outgoing=True)`."""

    def __init__(self, text: str):
        self.raw_text = text
        self.chat_id = -100123
        self.id = 1
        self.is_private = False
        self.client = SimpleNamespace(name="fake-client")
        self.edits: list[str] = []

    async def edit(self, text, **kwargs):
        self.edits.append(text)

    async def get_reply_message(self):
        return None


def _handle(tm, uid, text):
    return asyncio.run(tm._handle_modules(str(uid), FakeEvent(text),
                                          text.strip().split()[0].lower()))


def test_telethon_path_runs_module_command():
    from utils.telethon_manager import telethon_manager as tm

    _install(UID_A, "notes", SIMPLE)
    event = FakeEvent(".note купить хлеб")
    assert asyncio.run(tm._handle_modules(str(UID_A), event, ".note")) is True
    assert event.edits and "купить хлеб" in event.edits[0]


def test_telethon_path_ignores_other_users_module():
    """Ядро требования: команда модуля — только для сессии, её установившей.

    Модуль загружен для UID_A. Событие приходит от UID_B с тем же текстом:
    обработчик не должен вызваться, иначе юзер B получил бы чужую команду.
    """
    from utils.telethon_manager import telethon_manager as tm

    _install(UID_A, "notes", SIMPLE)
    event = FakeEvent(".note купить хлеб")
    assert asyncio.run(tm._handle_modules(str(UID_B), event, ".note")) is False
    assert event.edits == [], "модуль юзера A выполнился для юзера B"


def test_telethon_path_module_state_is_per_user(tmp_path, monkeypatch):
    """`ctx.state` тоже разделён: иначе модуль одного юзера видел бы
    состояние другого (курсор, избранные чаты)."""
    import config
    from utils.module_state import ModuleState

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    src = textwrap.dedent('''
        from slimbot_api import command

        MODULE = {"name": "Stateful", "version": "1"}

        @command(".counter")
        async def counter(ctx):
            n = ctx.state.get("n", 0) + 1
            ctx.state.set("n", n)
            return f"{ctx.user_id}:{n}"
    ''')
    _install(UID_A, "stateful", src)
    _install(UID_B, "stateful", src)
    spec_a = M.find_command(UID_A, ".counter")
    spec_b = M.find_command(UID_B, ".counter")

    async def call(uid):
        return await M.run_command(
            str(uid), spec_a if uid == UID_A else spec_b,
            Context(user_id=str(uid), state=ModuleState(str(uid), "stateful")),
        )

    assert asyncio.run(call(UID_A)) == f"{UID_A}:1"
    assert asyncio.run(call(UID_A)) == f"{UID_A}:2"
    assert asyncio.run(call(UID_B)) == f"{UID_B}:1", "счётчик юзера B стартовал не с нуля"


def test_telethon_path_passes_reply_and_client():
    src = textwrap.dedent('''
        from slimbot_api import command

        MODULE = {"name": "Ctx", "version": "1"}

        @command(".ctxprobe")
        async def probe(ctx):
            return f"client={ctx.client is not None} thread={ctx.thread_id} meta={ctx.meta['name']}"
    ''')
    from utils.telethon_manager import telethon_manager as tm

    _install(UID_A, "ctxprobe", src)
    event = FakeEvent(".ctxprobe")
    asyncio.run(tm._handle_modules(str(UID_A), event, ".ctxprobe", tid=7))
    assert "client=True" in event.edits[0]
    assert "thread=7" in event.edits[0]


def test_telethon_path_ignores_unknown_command():
    from utils.telethon_manager import telethon_manager as tm

    assert _handle(tm, UID_A, ".ping") is False
    assert _handle(tm, UID_A, ".note") is False


def test_disabled_system_command_is_swallowed():
    """Выключенная команда обязана молчать, а не подсказывать «возможно, .ping?»."""
    from utils.telethon_manager import telethon_manager as tm

    M.disable_system(UID_A, ".ping")
    assert _handle(tm, UID_A, ".ping") is True


def test_disabled_system_silent_only_for_its_owner():
    from utils.telethon_manager import telethon_manager as tm

    M.disable_system(UID_A, ".ping")
    assert _handle(tm, UID_B, ".ping") is False


def test_bot_only_command_is_skipped_in_telethon():
    src = textwrap.dedent('''
        from slimbot_api import command

        MODULE = {"name": "BotOnly", "version": "1"}

        @command(".bonly", paths=("bot",))
        async def bonly(ctx):
            return "bot only"
    ''')
    from utils.telethon_manager import telethon_manager as tm

    _install(UID_A, "bonly", src)
    assert _handle(tm, UID_A, ".bonly") is False


def test_hijack_needs_explicit_confirmation():
    """`.ping` из модуля НЕ перехватывается молча.

    Главный инвариант конфликт-механизма: пока юзер не выбрал «заменить»,
    системная команда остаётся системной.
    """
    from utils.telethon_manager import telethon_manager as tm

    _install(UID_A, "hijack", HIJACK)

    # Шаг 1: модуль установлен, решения нет.
    event = FakeEvent(".ping")
    assert asyncio.run(tm._handle_modules(str(UID_A), event, ".ping")) is False
    assert event.edits == []

    # Шаг 2: юзер выбрал «заменить».
    c = Conflict(uid=str(UID_A), module="hijack", head=".ping",
                 system_title="ping", module_desc="Свой пинг")
    M.register_conflict(c)
    assert M.resolve_conflict(c.key, "replace") == "replaced"

    event = FakeEvent(".ping")
    assert asyncio.run(tm._handle_modules(str(UID_A), event, ".ping")) is True
    assert event.edits == ["hijacked"]


def test_keep_after_replace_restores_system():
    """Юзер может передумать и вернуть системную команду."""
    from utils.telethon_manager import telethon_manager as tm

    _install(UID_A, "hijack", HIJACK)
    M.disable_system(str(UID_A), ".ping")
    assert _handle(tm, UID_A, ".ping") is True

    M.enable_system(str(UID_A), ".ping")
    event = FakeEvent(".ping")
    assert asyncio.run(tm._handle_modules(str(UID_A), event, ".ping")) is False


def test_modules_disabled_config_skips_everything(monkeypatch):
    import config

    from utils.telethon_manager import telethon_manager as tm

    _install(UID_A, "notes", SIMPLE)
    monkeypatch.setattr(config, "MODULES_ENABLED", False)
    assert _handle(tm, UID_A, ".note") is False


def test_denied_user_gets_no_module(monkeypatch):
    import config

    from utils.telethon_manager import telethon_manager as tm

    _install(UID_B, "notes", SIMPLE)
    monkeypatch.setattr(config, "MODULE_ALLOWLIST", {str(UID_A)})
    assert _handle(tm, UID_B, ".note") is False


# --------------------------------------------------------------------------
# Интеграция: inline-путь
# --------------------------------------------------------------------------


class FakeInline:
    def __init__(self, query: str, uid: int = UID_A):
        self.query = query
        self.from_user = SimpleNamespace(id=uid)
        self.bot = SimpleNamespace(name="bot")
        self.answers: list[dict] = []

    async def answer(self, results=None, **kwargs):
        self.answers.append({"results": list(results or []), **kwargs})
        return True


def _dispatch_inline(query: str, uid: int = UID_A) -> FakeInline:
    from handlers.inline import dispatch_inline
    from utils import cmds as _cmds

    M.set_system_heads(_cmds.CMDS)
    inline = FakeInline(query, uid)
    asyncio.run(dispatch_inline(inline, SimpleNamespace(name="bot")))
    return inline


def test_inline_module_command_answers():
    src = textwrap.dedent('''
        from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
        from slimbot_api import inline

        MODULE = {"name": "InlineNote", "version": "1"}

        @inline("memo", desc="Заметка")
        async def memo(ctx):
            return [InlineQueryResultArticle(
                id="memo",
                title="Заметка",
                description=ctx.args or "пусто",
                input_message_content=InputTextMessageContent(
                    message_text=f"<b>{ctx.args}</b>", parse_mode="HTML"),
            )]
    ''')
    _install(UID_A, "inlnote", src)
    inline = _dispatch_inline("memo купить хлеб")
    assert len(inline.answers) == 1
    assert inline.answers[0]["results"][0].title == "Заметка"


def test_inline_module_gets_rest_of_query_as_args():
    src = textwrap.dedent('''
        from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
        from slimbot_api import inline

        MODULE = {"name": "ArgsProbe", "version": "1"}

        @inline("probe")
        async def probe(ctx):
            return [InlineQueryResultArticle(
                id="p", title="args",
                input_message_content=InputTextMessageContent(message_text=ctx.args),
            )]
    ''')
    _install(UID_A, "argsprobe", src)
    inline = _dispatch_inline("probe раз два")
    assert inline.answers[0]["results"][0].input_message_content.message_text == "раз два"


def test_inline_module_returning_none_falls_back_to_help():
    """Модуль может не хотеть отвечать — тогда работает встроенная справка."""
    _install(UID_A, "notes", SIMPLE)  # его @inline("note") возвращает None
    inline = _dispatch_inline("note")
    assert inline.answers[0]["results"][0].id == "help_unknown_query"


def test_inline_module_of_other_user_not_called():
    _install(UID_A, "notes", SIMPLE)
    inline = _dispatch_inline("note", uid=UID_B)
    assert inline.answers[0]["results"][0].id == "help_unknown_query"


def test_builtin_inline_still_wins_by_default():
    """.статус остаётся системным, пока юзер не решил иначе."""
    _install(UID_A, "notes", SIMPLE)
    inline = _dispatch_inline("статус")
    assert inline.answers[0]["results"][0].id == "status_locked"


def test_inline_cache_time_comes_from_config():
    import config

    src = textwrap.dedent('''
        from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
        from slimbot_api import inline

        MODULE = {"name": "CacheProbe", "version": "1"}

        @inline("cacheprobe")
        async def probe(ctx):
            return [InlineQueryResultArticle(
                id="c", title="c",
                input_message_content=InputTextMessageContent(message_text="x"),
            )]
    ''')
    _install(UID_A, "cacheprobe", src)
    inline = _dispatch_inline("cacheprobe")
    assert inline.answers[0]["cache_time"] == config.INLINE_CACHE_DEFAULT


# --------------------------------------------------------------------------
# API-модуля: аргументы декларации
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", " ", "no dot?", ". with space"])
def test_command_rejects_bad_heads(bad):
    from utils.module_api import command

    if bad in ("", " "):
        with pytest.raises(ValueError):
            command(bad)
    else:
        with pytest.raises(ValueError):
            command(bad)


def test_command_accepts_head_without_dot():
    from utils.module_api import command

    dec = command("note", aliases=["n2"])
    fn = dec(lambda ctx: None)
    spec = fn._module_commands[0]
    assert spec.head == ".note"
    assert spec.aliases == (".n2",)


def test_command_normalizes_case():
    from utils.module_api import command

    dec = command(".NoTe")
    spec = dec(lambda ctx: None)._module_commands[0]
    assert spec.head == ".note", "head обязан быть в нижнем регистре"


def test_inline_keys_are_normalized():
    from utils.module_api import inline

    dec = inline("@Note", aliases=("n",))
    spec = dec(lambda ctx: None)._module_inline[0]
    assert spec.key == "note"
    assert spec.aliases == ("n",)


def test_module_paths_default_both():
    from utils.module_api import command

    spec = command(".x")(lambda ctx: None)._module_commands[0]
    assert spec.handler._module_paths == ("telethon", "bot")


# --------------------------------------------------------------------------
# rate-limit: бюджет модуля попадает в общий гейт
# --------------------------------------------------------------------------

RATED = textwrap.dedent('''
    from slimbot_api import command

    MODULE = {"name": "Rated", "version": "1"}

    @command(".slowcmd", rate="network")
    async def slow(ctx):
        return "ok"
''')


def test_rate_registers_budget_in_gate():
    from utils import rate_limit_gate as gate

    _install(UID_A, "rated", RATED)
    budget = gate.rate_limit_for(".slowcmd")
    assert budget is not None, "rate= не попал в гейт"
    assert budget[0] == "network"


def test_rate_limited_command_is_blocked(monkeypatch):
    """Третий вызов подряд блокируется тем же гейтом, что и `.net`."""
    import config
    from utils import rate_limit
    from utils import rate_limit_gate as gate

    monkeypatch.setattr(config, "EXPENSIVE_COMMAND_LIMIT", 2)
    monkeypatch.setattr(config, "EXPENSIVE_COMMAND_WINDOW", 60)
    rate_limit.clear(str(UID_A))
    _install(UID_A, "rated", RATED)

    assert gate.check(".slowcmd", str(UID_A)) is True
    assert gate.check(".slowcmd", str(UID_A)) is True
    assert gate.check(".slowcmd", str(UID_A)) is False
    rate_limit.clear(str(UID_A))


def test_unload_removes_budget():
    """Иначе бюджет «залипнет» и мёртвый head продолжит лимитироваться."""
    from utils import rate_limit_gate as gate

    _install(UID_A, "rated", RATED)
    assert gate.rate_limit_for(".slowcmd") is not None
    M.unload_module(UID_A, "rated")
    assert gate.rate_limit_for(".slowcmd") is None


def test_unknown_rate_is_ignored():
    """Опечатка в `rate=` не должна ломать загрузку и не даёт лимит."""
    from utils import rate_limit_gate as gate

    src = RATED.replace('rate="network"', 'rate="нету-такого"')
    _install(UID_A, "rated", src)
    assert gate.rate_limit_for(".slowcmd") is None


def test_rate_is_per_user():
    """Лимит общий на операцию, но бакет ключуется uid — как у встроенных."""
    from utils import rate_limit_gate as gate

    _install(UID_A, "rated", RATED)
    assert gate.rate_limit_for(".slowcmd") is not None
    assert gate.check(".slowcmd", str(UID_B)) is True, "у другого юзера свой бакет"


def test_telethon_path_applies_rate_gate(monkeypatch):
    import config
    from utils import rate_limit
    from utils import rate_limit_gate as gate
    from utils.telethon_manager import telethon_manager as tm

    monkeypatch.setattr(config, "EXPENSIVE_COMMAND_LIMIT", 1)
    monkeypatch.setattr(config, "EXPENSIVE_COMMAND_WINDOW", 60)
    rate_limit.clear(str(UID_A))
    _install(UID_A, "rated", RATED)

    event = FakeEvent(".slowcmd")
    assert asyncio.run(tm._handle_modules(str(UID_A), event, ".slowcmd")) is True
    assert event.edits == ["ok"]

    blocked = FakeEvent(".slowcmd")
    assert asyncio.run(tm._handle_modules(str(UID_A), blocked, ".slowcmd")) is True
    assert blocked.edits == [gate.RATE_LIMIT_TEXT]
    rate_limit.clear(str(UID_A))


def test_command_requires_at_least_one_head():
    from utils.module_api import command

    with pytest.raises(ValueError, match="хотя бы одну"):
        command()


# --------------------------------------------------------------------------
# Состояние модуля
# --------------------------------------------------------------------------


def test_module_state_roundtrip(tmp_path, monkeypatch):
    import config
    from utils.module_state import ModuleState

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    st = ModuleState(str(UID_A), "notes")
    st.set("cursor", 5)
    st.update({"a": 1, "b": "два"})
    assert st.get("cursor") == 5
    assert st.get("missing", "def") == "def"
    assert st.all() == {"cursor": 5, "a": 1, "b": "два"}
    st.delete("cursor")
    assert st.get("cursor") is None
    st.clear()
    assert st.all() == {}


def test_module_state_survives_broken_json(tmp_path, monkeypatch):
    import config
    from utils.module_state import ModuleState, state_path

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    path = state_path(str(UID_A), "notes")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{не json", encoding="utf-8")
    st = ModuleState(str(UID_A), "notes")
    assert st.all() == {}, "битый state не должен ронять команду модуля"
    st.set("ok", 1)
    assert st.get("ok") == 1


def test_module_state_is_per_user(tmp_path, monkeypatch):
    import config
    from utils.module_state import ModuleState

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    ModuleState(str(UID_A), "notes").set("k", "a")
    assert ModuleState(str(UID_B), "notes").get("k") is None


# --------------------------------------------------------------------------
# Старт: модули поднимаются при запуске
# --------------------------------------------------------------------------


def test_load_user_modules_respects_enabled_flag():
    from utils.storage import set_module_enabled

    _install(UID_A, "notes", SIMPLE)
    M.unload_module(UID_A, "notes")
    set_module_enabled(str(UID_A), "notes", False)
    assert M.load_user_modules(str(UID_A)) == []
    assert M.find_command(UID_A, ".note") is None, "выключенный модуль не грузится"


def test_load_user_modules_collects_errors():
    M.save_module_source(UID_A, "broken", BROKEN)
    from utils.storage import register_module

    register_module(str(UID_A), "broken")
    errors = M.load_user_modules(str(UID_A))
    assert errors and "broken" in errors[0]


def test_startup_helper_survives_broken_module():
    """Плохой модуль не должен мешать старту бота."""
    import bot

    M.save_module_source(UID_A, "broken", BROKEN)
    from utils.storage import register_module

    register_module(str(UID_A), "broken")
    bot._start_modules()  # не бросает


# --------------------------------------------------------------------------
# aiogram-слой: роутер и хендлеры
# --------------------------------------------------------------------------


def test_modules_router_has_no_own_handlers_on_parent():
    """Инвариант 1: message-хендлеры живут на суб-роутере, не на `router`."""
    import handlers.commands as C

    assert not C.router.message.handlers
    assert C.router.sub_routers[-1] is C._fallback_router


def test_modules_router_is_before_fallback():
    import handlers.commands as C

    names = [getattr(r, "name", "") for r in C.router.sub_routers]
    assert "commands-fallback" in names
    mod_idx = names.index("modules") if "modules" in names else None
    assert mod_idx is not None, "роутер модулей должен быть подключён"
    assert mod_idx < names.index("commands-fallback")


def test_source_has_no_conflict_handler_on_wrong_router():
    import inspect

    from handlers.commands import modules as modules_mod

    src = inspect.getsource(modules_mod)
    assert 'callback_query(F.data.startswith("mcf:"))' in src


def test_bot_path_dispatches_module_command():
    from handlers.commands import modules as modules_mod

    _install(UID_A, "notes", SIMPLE)
    sent: list[str] = []

    class Msg:
        text = ".note привет"
        from_user = SimpleNamespace(id=UID_A)
        bot = SimpleNamespace(name="bot")

        async def reply(self, text, **kwargs):
            sent.append(text)

    asyncio.run(modules_mod.dispatch_module_command(Msg()))
    assert sent and "привет" in sent[0]


def test_bot_path_ignores_other_users_module():
    """Та же изоляция в личке с ботом: модуль A не должен ловить `.note` у B."""
    from handlers.commands import modules as modules_mod

    _install(UID_A, "notes", SIMPLE)
    sent: list[str] = []

    class Msg:
        text = ".note привет"
        from_user = SimpleNamespace(id=UID_B)
        bot = SimpleNamespace(name="bot")

        async def reply(self, text, **kwargs):
            sent.append(text)

    asyncio.run(modules_mod.dispatch_module_command(Msg()))
    assert sent == [], "модуль юзера A выполнился в личке юзера B"


def test_bot_path_disabled_system_is_not_claimed():
    """Выключенная юзером системная команда не уходит в модуль молча."""
    from handlers.commands import modules as modules_mod

    _install(UID_A, "hijack", HIJACK)
    sent: list[str] = []

    class Msg:
        text = ".ping"
        from_user = SimpleNamespace(id=UID_A)
        bot = SimpleNamespace(name="bot")

        async def reply(self, text, **kwargs):
            sent.append(text)

    # Юзер решил оставить системный .ping: fallback должен предложить
    # подсказку, но не отправлять «hijacked».
    M.enable_system(str(UID_A), ".ping")
    asyncio.run(modules_mod.dispatch_module_command(Msg()))
    assert "hijacked" not in sent

    # А после явного «заменить» — уже выполняется.
    M.disable_system(str(UID_A), ".ping")
    sent.clear()
    asyncio.run(modules_mod.dispatch_module_command(Msg()))
    assert sent == ["hijacked"]


def test_bot_path_ignores_non_module_command():
    from handlers.commands import modules as modules_mod

    called: list[str] = []

    class Msg:
        text = ".ping"
        from_user = SimpleNamespace(id=UID_A)

        async def reply(self, text, **kwargs):
            called.append(text)

    asyncio.run(modules_mod.dispatch_module_command(Msg()))
    assert called == [], ".ping без решения по конфликту не должен уйти в модуль"