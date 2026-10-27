"""Entry point: wires the app together and runs Telegram long polling.

This is the scaffold stage. The dispatcher is created and started, but routers,
the whitelist middleware, and the rate-limit middleware are registered in the
security/UI stages. Until then the bot polls and safely ignores every update
(no handlers => no responses), which matches the "whitelist-only" policy.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.admin.router import build_admin_router
from bot.alerts import Alerter
from bot.botui import accounts as accounts_handler
from bot.botui import menu as menu_handler
from bot.botui import referrals as referrals_handler
from bot.botui import stats as stats_handler
from bot.botui import wallet as wallet_handler
from bot.botui import withdraw as withdraw_handler
from bot.botui.middleware import DbSessionMiddleware
from bot.config import Settings, get_settings
from bot.db.engine import make_engine, make_sessionmaker
from bot.db.settings import DbSettings
from bot.exchanges.registry import build_adapters
from bot.handlers import credentials as credentials_handler
from bot.handlers import errors as errors_handler
from bot.i18n.middleware import LocaleMiddleware
from bot.i18n.router import router as language_router
from bot.invites.router import router as invites_router
from bot.logging import configure_logging, get_logger
from bot.notifications import poller as notif_poller
from bot.partners.router import router as partners_router
from bot.security.access import BlockGuardMiddleware
from bot.security.credential_rotation import CredentialRotationService, UnixCredentialUpdater
from bot.security.ratelimit import RateLimitMiddleware
from bot.security.secrets import SecretCipher
from bot.support.router import router as support_router

logger = get_logger(__name__)


def build_dispatcher(settings: Settings) -> Dispatcher:
    """Create the dispatcher with security middleware, routers, and adapters."""
    dp = Dispatcher()

    # Adapters are injected into every handler via workflow data.
    dp["adapters"] = build_adapters(settings)
    dp["credential_rotation"] = None
    if settings.credential_update_enabled:
        updater = UnixCredentialUpdater(
            settings.credential_updater_socket,
            timeout_seconds=settings.credential_updater_timeout_seconds,
        )
        dp["credential_rotation"] = CredentialRotationService(settings, updater)

    # Admin subset (Phase 6): injected so botui handlers can bootstrap the first
    # admin(s) via ensure_role, and so build_admin_router below can gate its router.
    dp["admin_ids"] = settings.admin_telegram_ids

    # One DB engine/sessionmaker for the process lifetime; `make_engine` is lazy
    # (no connection at construction), so this stays safe even without a live DB.
    engine = make_engine(DbSettings().database_url)
    sessionmaker = make_sessionmaker(engine)
    dp["db_engine"] = engine  # kept for disposal on shutdown, see run()
    dp["sessionmaker"] = sessionmaker  # used by the notification poller in run()

    # Wallet encryption is optional: with no MASTER_KEY, wallet handlers degrade
    # with a clear message instead of crashing (see bot/botui/wallet.py).
    dp["cipher"] = (
        SecretCipher(settings.master_key.get_secret_value()) if settings.master_key else None
    )

    # The bot is open to everyone (public referral-rebate cabinet); throttle first so
    # a flood — including from a blocked user — is bounded before any DB work.
    # One limiter instance shared across messages and callbacks => unified per-user budget.
    credential_states = (
        credentials_handler.CredentialFlow.choosing_exchange.state,
        credentials_handler.CredentialFlow.entering_uid.state,
        credentials_handler.CredentialFlow.entering_credentials.state,
        credentials_handler.CredentialFlow.applying.state,
    )
    if any(state is None for state in credential_states):
        raise RuntimeError("Credential FSM states are not initialized.")
    rate_limiter = RateLimitMiddleware(
        settings.rate_limit_seconds,
        bypass_states=frozenset(state for state in credential_states if state is not None),
    )
    dp.message.middleware(rate_limiter)
    dp.callback_query.middleware(rate_limiter)

    # DB session middleware is inner (after rate-limit): one AsyncSession per update.
    db_middleware = DbSessionMiddleware(sessionmaker)
    dp.message.middleware(db_middleware)
    dp.callback_query.middleware(db_middleware)

    # Block guard runs AFTER the DB session (it needs users.status): turns away only
    # admin-blocked accounts; admins + developer are exempt so staff can never be
    # locked out. Access is otherwise open — env IDs are roles, not an allow-list.
    staff_ids = settings.admin_telegram_ids | {settings.developer_telegram_id}
    locale_middleware = LocaleMiddleware(settings.admin_telegram_ids)
    dp.message.middleware(locale_middleware)
    dp.callback_query.middleware(locale_middleware)
    block_guard = BlockGuardMiddleware(staff_ids)
    dp.message.middleware(block_guard)
    dp.callback_query.middleware(block_guard)

    # The cabinet `/start` is the primary (and only) entry point. The legacy
    # commission-calculator UI has been removed; only bot/handlers/errors.py remains.
    dp.include_router(menu_handler.router)
    dp.include_router(language_router)
    dp.include_router(wallet_handler.router)
    dp.include_router(accounts_handler.router)
    dp.include_router(stats_handler.router)
    dp.include_router(referrals_handler.router)
    dp.include_router(withdraw_handler.router)
    dp.include_router(support_router)
    dp.include_router(invites_router)
    dp.include_router(partners_router)
    dp.include_router(credentials_handler.build_credentials_router(settings.admin_telegram_ids))
    # Admin panel: gated by IsAdmin(admin_telegram_ids) inside build_admin_router itself.
    # After the botui routers (whitelist-only cabinet flows), before the catch-all errors router.
    dp.include_router(build_admin_router(settings.admin_telegram_ids))
    dp.include_router(errors_handler.router)
    return dp


async def run() -> None:
    settings = get_settings()
    configure_logging()

    exchanges = settings.available_exchanges
    logger.info(
        "starting bot",
        available_exchanges=sorted(exchanges),
        access="open",
        admin_count=len(settings.admin_telegram_ids),
    )
    if not exchanges:
        logger.warning("no exchanges configured — set API credentials in the environment")

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher(settings)
    dp["alerter"] = Alerter(bot, settings.developer_telegram_id)

    # Deliver the notification outbox (payout-paid/failed to users, alerts to the
    # developer) that the tokenless executor/accrual processes enqueue in the DB.
    poller_task = asyncio.create_task(
        notif_poller.run_forever(
            bot,
            dp["sessionmaker"],
            chain_id=settings.bsc_chain_id,
            sleep=asyncio.sleep,
            now=lambda: datetime.now(UTC),
        )
    )

    try:
        # Drop updates accumulated while the bot was offline.
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        poller_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller_task
        for adapter in dp["adapters"].values():
            aclose = getattr(adapter, "aclose", None)
            if aclose is not None:
                await aclose()
        await dp["db_engine"].dispose()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("shutdown requested")


if __name__ == "__main__":
    main()
