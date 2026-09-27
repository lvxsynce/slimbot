"""Logging setup with file rotation (bot.log не растёт бесконечно)."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 5


def setup_file_logging(path: str | Path, max_bytes: int = DEFAULT_MAX_BYTES, backups: int = DEFAULT_BACKUPS) -> RotatingFileHandler:
    """Файловый handler с ротацией. Идемпотентен по пути (дубли не вешает)."""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler):
            try:
                if Path(handler.baseFilename) == log_path.resolve():
                    return handler
            except OSError:
                pass
    file_handler = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)
    return file_handler
