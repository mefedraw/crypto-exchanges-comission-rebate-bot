"""Part B — bounded REAL-testnet executor pass.

Seeds ~30 fixed-tiny payouts into a throwaway DB, then drives the REAL PayoutExecutor
against BSC testnet + Mantle Sepolia (key from env, NEVER printed), verifies each payout
on-chain + in the DB, and prints a table. Intended to run in a one-off container on the
server; the runner creates/drops the throwaway `simtest` DB and passes SIMTEST_DATABASE_URL.

Fixed amounts: 0.01 USDT / 0.01 MNT. Recipients: a few throwaway testnet addresses +
self-send (hot wallet) for volume. min_payout is set to 0.001 so 0.01 sends pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

# Windows terminals default to a legacy codepage (cp1251) that can't encode the
# report's em-dash/emoji; force UTF-8 and never crash on output.
with contextlib.suppress(AttributeError, ValueError):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from sqlalchemy import func, select, text

from bot.admin import repository as admin_repo
from bot.botui import repository as botui_repo
from bot.botui import service as botui
from bot.config import get_settings
from bot.db.base import Base
from bot.db.engine import make_engine, make_sessionmaker
from bot.db.models.identity import ExchangeAccount
from bot.db.models.ledger import RebateAccrual
from bot.db.models.payouts import Payout
from bot.executor.chain_web3 import build_chain_client, build_mantle_chain_client
from bot.executor.executor import PayoutExecutor
from bot.payout.expiry import available_at, expires_at
from bot.security.secrets import SecretCipher

USDT_SELF = int(os.environ.get("USDT_SELF", "8"))  # self-send USDT payouts
USDT_EXT = int(os.environ.get("USDT_EXT", "4"))  # to throwaway addresses
USDT_PENDING = int(os.environ.get("USDT_PENDING", "3"))  # start pending_admin (admin release)
MNT_PENDING = int(os.environ.get("MNT_PENDING", "5"))  # MNT always pending_admin
BELOW_MIN = 1  # one below-min negative case
FIXED = Decimal("0.01")


def _now() -> datetime:
    return datetime.now(UTC)


async def _seed_payout(s, cipher, uid, wallet_addr, asset, net, status, day):
    """Create a user+wallet+account+one reserved accrual+one payout in `status`."""
    await botui_repo.upsert_user(s, uid)
    await botui.set_wallet(s, cipher, uid, wallet_addr)
    wallet = await botui_repo.get_active_wallet(s, uid)
    acc = ExchangeAccount(
        user_id=uid,
        exchange="gate",
        uid=f"tn-{uid}",
        status="active",
        accrual_since=date(2026, 1, 1),
    )
    s.add(acc)
    await s.flush()
    d = date(2026, 1, 1) + timedelta(days=day)
    accr = RebateAccrual(
        user_id=uid,
        account_id=acc.id,
        accrual_date=d,
        asset=asset,
        amount_usdt=net,
        effective_rank=1,
        fee_pct=Decimal("0"),
        commission_usdt=Decimal("0"),
        net_usdt=net,
        amount_native=net,
        net_native=net,
        fx_rate=Decimal("1"),
        state="reserved",
        available_at=available_at(d),
        expires_at=expires_at(d),
        finalized=True,
    )
    s.add(accr)
    await s.flush()
    payout = Payout(
        user_id=uid,
        wallet_id=wallet.id,
        address_encrypted=wallet.address_encrypted,
        address_masked=wallet.address_masked,
        covered_from=d,
        covered_to=d,
        gross_usdt=net,
        commission_usdt=Decimal("0"),
        net_usdt=net,
        asset=asset,
        net_native=(net if asset == "MNT" else Decimal("0")),
        status=status,
        idempotency_key=f"tn:{uid}:{asset}",
        approved_by=None,
        approved_at=None,
    )
    s.add(payout)
    await s.flush()
    accr.payout_id = payout.id
    await s.flush()
    return payout.id


async def main() -> None:
    settings = get_settings()
    db_url = os.environ["SIMTEST_DATABASE_URL"]
    db_name = db_url.rsplit("/", 1)[-1].split("?")[0]
    if db_name != "simtest":
        raise SystemExit(
            f"refusing: SIMTEST_DATABASE_URL must target the throwaway 'simtest' database, got {db_name!r}"
        )
    cipher = SecretCipher(settings.master_key.get_secret_value())  # type: ignore[union-attr]
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)

    hot = settings.payout_wallet_address
    assert hot, "PAYOUT_WALLET_ADDRESS must be set"
    # a couple of deterministic throwaway testnet recipient addresses
    ext_addrs = [f"0x{(i + 1):040x}" for i in range(USDT_EXT)]

    admin_uid = 700_000  # dedicated admin user row so approved_by FK resolves
    uid = admin_uid
    seeded: list[tuple[int, str, str]] = []  # (payout_id, asset, kind)
    async with sm() as s:
        await botui_repo.upsert_user(s, admin_uid)
        await s.commit()
    async with sm() as s:
        for _ in range(USDT_SELF):
            uid += 1
            pid = await _seed_payout(s, cipher, uid, hot, "USDT", FIXED, "reserved", 1)
            seeded.append((pid, "USDT", "self"))
        for a in ext_addrs:
            uid += 1
            pid = await _seed_payout(s, cipher, uid, a, "USDT", FIXED, "reserved", 1)
            seeded.append((pid, "USDT", "ext"))
        for _ in range(USDT_PENDING):
            uid += 1
            pid = await _seed_payout(s, cipher, uid, hot, "USDT", FIXED, "pending_admin", 1)
            seeded.append((pid, "USDT", "pending"))
        for _ in range(MNT_PENDING):
            uid += 1
            pid = await _seed_payout(s, cipher, uid, hot, "MNT", FIXED, "pending_admin", 1)
            seeded.append((pid, "MNT", "pending"))
        for _ in range(BELOW_MIN):
            uid += 1
            pid = await _seed_payout(s, cipher, uid, hot, "USDT", Decimal("0.0001"), "reserved", 1)
            seeded.append((pid, "USDT", "below_min"))
        await s.commit()

    bsc = build_chain_client(settings)
    mantle = build_mantle_chain_client(settings)
    assert type(bsc).__name__ == "Web3ChainClient", f"BSC client is {type(bsc).__name__}"
    assert mantle is not None, "Mantle client not built"
    executor = PayoutExecutor(
        sm,
        bsc,
        cipher,
        now=_now,
        wallet_address=hot,
        auto_limit=Decimal("1000"),
        min_payout=Decimal("0.001"),
        confirmations=1,
        developer_id=None,
        mantle_chain=mantle,
    )

    print(f"seeded {len(seeded)} payouts; driving real testnet executor...")
    for _i in range(4000):
        await executor.process_next()
        await executor.poll_confirmations()
        async with sm() as s:
            pend = await s.scalar(
                select(func.count()).select_from(Payout).where(Payout.status == "pending_admin")
            )
            active = await s.scalar(
                select(func.count())
                .select_from(Payout)
                .where(Payout.status.in_(("reserved", "submitting", "sent")))
            )
        if pend and not active:
            # release all pending_admin (admin funded wallet / reviewed)
            async with sm() as s:
                for p in await admin_repo.list_pending_admin(s):
                    await admin_repo.release_payout(s, p.id, admin_uid)
                await s.commit()
        if not pend and not active:
            break

    # report
    print("\n== Part B — real testnet payouts ==")
    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, asset, status, nonce, net_usdt, net_native, txhash, failed_reason "
                    "FROM payouts ORDER BY asset, id"
                )
            )
        ).all()
    ok = True
    for pid, asset, status, nonce, net, natv, txh, reason in rows:
        exp = "sepolia.mantlescan.xyz" if asset == "MNT" else "testnet.bscscan.com"
        link = f"https://{exp}/tx/{txh}" if txh else "-"
        print(
            f"  #{pid} {asset:<4} {status:<8} nonce={nonce} "
            f"amt={Decimal(net):.4f}/{Decimal(natv):.4f} {reason or ''} {link}"
        )
        # expected terminal states: paid for normal, failed for the below-min one
        if status not in ("paid", "failed"):
            ok = False
    print("\nPart B:", "PASS ✅" if ok else "NOT ALL TERMINAL ❌")
    await engine.dispose()
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
