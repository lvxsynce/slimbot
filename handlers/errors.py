import logging

from aiogram import Router, types

logger = logging.getLogger(__name__)
router = Router()


@router.errors()
async def errors_handler(event: types.ErrorEvent):
    logger.exception(
        "Unhandled error: %s",
        event.exception,
        exc_info=event.exception,
    )
