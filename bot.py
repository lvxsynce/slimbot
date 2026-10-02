import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from config import TOKEN, BOT_NAME, SESSIONS_DIR, TEMP_DIR, DATA_DIR
import config as cfg

logging.basicConfig(level=logging.WARNING)
logging.getLogger("aiogram.event").setLevel(logging.INFO)
logging.getLogger("utils.telethon_manager").setLevel(logging.INFO)
logging.getLogger("handlers.session").setLevel(logging.DEBUG)

from utils.logging_setup import setup_file_logging
setup_file_logging(DATA_DIR / "bot.log")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
_background_tasks: set[asyncio.Task] = set()

from handlers.commands import router as commands_router
from handlers.errors import router as errors_router
from handlers.session import router as session_router
from handlers.inline import router as inline_router, mark_bot_started
from utils.storage import user_sessions

dp.include_router(commands_router)
dp.include_router(inline_router)
dp.include_router(session_router)
dp.include_router(errors_router)


def _start_modules() -> None:
    """Поднять сохранённые модули всех юзеров.

    Отдельная функция (а не тело `on_startup`), потому что сюда нельзя
    `await`: модуль — обычный импорт Python, он не ждёт.
    """
    import logging as _logging

    log = _logging.getLogger(__name__)
    try:
        from utils import modules as M
        from utils import cmds
        from utils import storage

        if not M.modules_enabled():
            log.info("modules: disabled via config")
            return
        M.set_system_heads(cmds.CMDS)
        total = 0
        for uid in sorted(storage.module_settings):
            errors = M.load_user_modules(uid)
            total += len(M.list_modules(uid))
            for err in errors:
                log.warning("modules: uid=%s %s", uid, err)
        log.info("modules: %d loaded across %d users", total, len(storage.module_settings))
    except Exception:
        log.exception("modules: startup failed — бот продолжит без модулей")


@dp.startup()
async def on_startup():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    me = await bot.get_me()
    cfg.BOT_USERNAME = me.username
    print(f"@{me.username} ({BOT_NAME}) запущен...")

    # Модули: сообщаем загрузчику системные команды (нужно для проверки
    # конфликтов «свой .ping» vs системный) и поднимаем то, что юзеры
    # ставили в прошлых сессиях. Ошибка одного модуля не должна мешать
    # старту бота — поэтому всё под try/except.
    _start_modules()

    from utils.telethon_manager import telethon_manager, auth_state_cleaner, cleanup_orphan_sessions
    cleanup_orphan_sessions()
    telethon_manager.set_bot(bot)
    await telethon_manager.start_all_active()
    telethon_manager.start_health_check()
    active = len(telethon_manager._clients)
    logging.getLogger(__name__).info(
        "Runtime state: data_dir=%s, persisted_sessions=%s, restored_clients=%s",
        cfg.DATA_DIR,
        sum(
            1 for value in user_sessions.values()
            if isinstance(value, dict) and value.get("status") == "active"
        ),
        active,
    )
    print(f"Telethon-клиентов запущено: {active}")
    auth_task = asyncio.create_task(auth_state_cleaner())
    _background_tasks.add(auth_task)
    auth_task.add_done_callback(_background_tasks.discard)
    mark_bot_started()


@dp.shutdown()
async def on_shutdown():
    from utils.telethon_manager import telethon_manager
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    _background_tasks.clear()
    try:
        await telethon_manager.stop_health_check()
        await telethon_manager.stop_all()
    except Exception as e:
        logging.getLogger(__name__).warning(f"on_shutdown: stop_all failed: {e}")
    try:
        await bot.session.close()
    except Exception as e:
        logging.getLogger(__name__).warning("on_shutdown: bot session close failed: %s", e)
    print("Telethon-клиенты остановлены.")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
