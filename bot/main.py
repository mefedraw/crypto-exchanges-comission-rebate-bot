"""Entry point: wires the app together and runs Telegram long polling.

This is the scaffold stage. The dispatcher is created and started, but routers,
the whitelist middleware, and the rate-limit middleware are registered in the
security/UI stages. Until then the bot polls and safely ignores every update
(no handlers => no responses), which matches the "whitelist-only" policy.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.alerts import Alerter
from bot.config import Settings, get_settings
from bot.exchanges.registry import build_adapters
from bot.handlers import errors as errors_handler
from bot.handlers import flow as flow_handler
from bot.handlers import start as start_handler
from bot.logging import configure_logging, get_logger
from bot.security.access import WhitelistMiddleware
from bot.security.ratelimit import RateLimitMiddleware

logger = get_logger(__name__)


def build_dispatcher(settings: Settings) -> Dispatcher:
    """Create the dispatcher with security middleware, routers, and adapters."""
    dp = Dispatcher()

    # Adapters are injected into every handler via workflow data.
    dp["adapters"] = build_adapters(settings)

    # Security first: reject non-whitelisted users before anything else, then throttle.
    # One limiter instance shared across messages and callbacks => unified per-user budget.
    dp.update.outer_middleware(WhitelistMiddleware(settings.allowed_telegram_ids))
    rate_limiter = RateLimitMiddleware(settings.rate_limit_seconds)
    dp.message.middleware(rate_limiter)
    dp.callback_query.middleware(rate_limiter)

    # Commands before the FSM flow; error router last.
    dp.include_router(start_handler.router)
    dp.include_router(flow_handler.router)
    dp.include_router(errors_handler.router)
    return dp


async def run() -> None:
    settings = get_settings()
    configure_logging()

    exchanges = settings.available_exchanges
    logger.info(
        "starting bot",
        available_exchanges=sorted(exchanges),
        whitelisted_user_count=len(settings.allowed_telegram_ids),
    )
    if not exchanges:
        logger.warning("no exchanges configured — set API credentials in the environment")

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher(settings)
    dp["alerter"] = Alerter(bot, settings.developer_telegram_id)

    try:
        # Drop updates accumulated while the bot was offline.
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        for adapter in dp["adapters"].values():
            aclose = getattr(adapter, "aclose", None)
            if aclose is not None:
                await aclose()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("shutdown requested")


if __name__ == "__main__":
    main()
