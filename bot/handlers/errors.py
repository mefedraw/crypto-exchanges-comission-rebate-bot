"""Global error handler so an unexpected failure never crashes the bot.

Logs the traceback (structlog redacts sensitive keys) and shows the user a
neutral message. No exception detail is leaked to the chat.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, ErrorEvent, Message

from bot.alerts import Alerter
from bot.logging import get_logger

logger = get_logger(__name__)

router = Router(name="errors")

_USER_MESSAGE = "Что-то пошло не так. Попробуйте /start и повторите."


@router.errors()
async def handle_error(event: ErrorEvent, alerter: Alerter) -> bool:
    logger.error(
        "unhandled error",
        error=type(event.exception).__name__,
        update_type=type(event.update.event).__name__,
        exc_info=event.exception,
    )

    # Notify the developer about every unhandled failure.
    await alerter.alert_exception(
        "Необработанная ошибка",
        event.exception,
        context=f"update: {type(event.update.event).__name__}",
    )

    update_event = event.update.event
    try:
        if isinstance(update_event, CallbackQuery):
            await update_event.answer(_USER_MESSAGE, show_alert=True)
        elif isinstance(update_event, Message):
            await update_event.answer(_USER_MESSAGE)
    except Exception:  # noqa: BLE001 - best-effort notification; never re-raise
        logger.warning("failed to notify user about error")

    return True  # mark as handled
