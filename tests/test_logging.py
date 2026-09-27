"""Логи: ротация bot.log."""

import logging

from utils.logging_setup import setup_file_logging


def test_file_logging_rotates(tmp_path):
    path = tmp_path / "bot.log"
    handler = setup_file_logging(path, max_bytes=300, backups=2)
    try:
        logger = logging.getLogger("test-rotate-probe")
        for i in range(100):
            logger.warning("probe line %03d %s", i, "x" * 40)
        for h in logging.getLogger().handlers:
            h.flush()
        names = {p.name for p in tmp_path.iterdir()}
        assert "bot.log" in names
        assert "bot.log.1" in names
        assert path.stat().st_size < 300 + 200
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def test_setup_is_idempotent(tmp_path):
    path = tmp_path / "bot2.log"
    h1 = setup_file_logging(path)
    h2 = setup_file_logging(path)
    try:
        assert h1 is h2
    finally:
        logging.getLogger().removeHandler(h1)
        h1.close()
