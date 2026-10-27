"""Payout-executor process entrypoint: ``python -m bot.executor``.

Runs SEPARATELY from the Telegram bot (security §2): this process holds the payout
wallet key (via the real ChainClient) and no Telegram token; the bot process holds the
token and no key. Without PAYOUT_PRIVATE_KEY/BSC_RPC_URLS/USDT_CONTRACT the executor
falls back to MockChainClient and performs no on-chain sends (safe for dev).

This module is thin runtime wiring (like ``bot.main.run``) and is exercised end-to-end
only against a real DB/chain at deploy; the executor's logic is unit-tested elsewhere.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bot.botui.service import MIN_PAYOUT_USDT
from bot.config import get_settings
from bot.db.engine import make_engine, make_sessionmaker
from bot.db.settings import DbSettings
from bot.executor.chain import MockChainClient
from bot.executor.chain_web3 import build_chain_client, build_mantle_chain_client
from bot.executor.executor import PayoutExecutor
from bot.executor.scheduler import ExecutorScheduler
from bot.logging import configure_logging, get_logger
from bot.security.secrets import SecretCipher

logger = get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging()

    if settings.master_key is None:
        # The executor must decrypt each payout's recipient address; without the master
        # key it cannot operate. Fail closed rather than run blind.
        logger.error("executor cannot start: MASTER_KEY is not set")
        return

    cipher = SecretCipher(settings.master_key.get_secret_value())
    chain = build_chain_client(settings)
    # MNT payouts are feature-flagged by MANTLE_RPC_URL (settings.mnt_enabled); absent
    # => None, and PayoutExecutor fails MNT payouts defensively (mnt_disabled).
    mantle_chain = build_mantle_chain_client(settings)
    wallet_address = (
        getattr(chain, "address", None) or settings.payout_wallet_address or MockChainClient.WALLET
    )
    logger.info(
        "starting payout-executor",
        chain=type(chain).__name__,
        wallet=wallet_address[:6] + "…" if wallet_address else None,
        auto_limit=str(settings.auto_limit_usdt),
        confirmations=settings.confirmations,
        mnt_enabled=settings.mnt_enabled,
    )

    engine = make_engine(DbSettings().database_url)
    sessionmaker = make_sessionmaker(engine)
    executor = PayoutExecutor(
        sessionmaker,
        chain,
        cipher,
        now=lambda: datetime.now(UTC),
        wallet_address=wallet_address,
        auto_limit=settings.auto_limit_usdt,
        min_payout=MIN_PAYOUT_USDT,
        confirmations=settings.confirmations,
        developer_id=settings.developer_telegram_id,
        mantle_chain=mantle_chain,
    )
    scheduler = ExecutorScheduler(
        executor,
        sleep=asyncio.sleep,
        now=lambda: datetime.now(UTC),
    )
    try:
        await scheduler.run_forever()
    finally:
        await engine.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("executor shutdown requested")


if __name__ == "__main__":
    main()
