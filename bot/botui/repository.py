# bot/botui/repository.py
"""Async DB access for the self-service cabinet. Decimal money; thin over models."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import ActionToken, AuditEvent
from bot.db.models.payouts import Payout

_SUCCESS_STATUSES = ("sent", "confirmed", "paid")


async def _clear_stale_username_holders(
    session: AsyncSession, username: str, keep_user_id: int
) -> None:
    """FIX M2: Telegram usernames are unique only at any instant — a user can drop a
    username and another user can pick it up later. A stale row that still carries
    the old username would make ``find_user_id_by_username`` resolve to the WRONG
    (former) user. Clear ``username`` (case-insensitively) from every OTHER user row
    before assigning it here, so at most one user ever holds a given username."""
    await session.execute(
        update(User)
        .where(func.lower(User.username) == username, User.id != keep_user_id)
        .values(username=None)
    )


async def upsert_user(session: AsyncSession, user_id: int, username: str | None = None) -> User:
    """Fetch-or-create the ``User`` row for ``user_id``.

    ``username`` (Telegram's, no leading ``@``) is stored lowercased whenever provided —
    on both first creation and a returning user's refresh — so lookups via
    ``find_user_id_by_username`` are case-insensitive. Telegram only supplies a
    username for users who have messaged the bot; when ``None`` (unset, or the caller
    doesn't have it), the previously stored value is left untouched rather than wiped.
    """
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, role="user", status="active")
        if username is not None:
            uname = username.lower()
            await _clear_stale_username_holders(session, uname, user_id)
            user.username = uname
        session.add(user)
        await session.flush()
        return user
    if username is not None:
        uname = username.lower()
        await _clear_stale_username_holders(session, uname, user_id)
        user.username = uname
    return user


async def find_user_id_by_username(session: AsyncSession, username: str) -> int | None:
    """Resolve a Telegram ``@nick`` (or bare ``nick``) to its ``user_id``, case-insensitively."""
    stmt = select(User.id).where(func.lower(User.username) == username.strip().lstrip("@").lower())
    return cast("int | None", await session.scalar(stmt))


async def ensure_role(session: AsyncSession, user_id: int, admin_ids: frozenset[int]) -> None:
    """Idempotently promote ``user_id`` to ``role='admin'`` when env-listed.

    First-admin bootstrap (Phase 6 Task 7): ``ADMIN_TELEGRAM_IDS`` is the only way to
    grant the very first admin (there is no one to grant it in-bot yet). Users not in
    ``admin_ids`` are left untouched — this never demotes anyone. Call after
    ``upsert_user`` so the row already exists.
    """
    if user_id not in admin_ids:
        return
    await session.execute(
        update(User).where(User.id == user_id, User.role != "admin").values(role="admin")
    )


async def get_user_status(session: AsyncSession, user_id: int) -> str | None:
    """``users.status`` for ``user_id`` (e.g. ``active``/``blocked``), or ``None`` if unknown."""
    stmt = select(User.status).where(User.id == user_id)
    return cast("str | None", await session.scalar(stmt))


async def get_active_wallet(session: AsyncSession, user_id: int) -> Wallet | None:
    stmt = select(Wallet).where(Wallet.user_id == user_id, Wallet.status == "active")
    return cast("Wallet | None", await session.scalar(stmt))


async def replace_wallet(
    session: AsyncSession,
    user_id: int,
    *,
    address_encrypted: bytes,
    address_hash: bytes,
    address_masked: str,
) -> Wallet:
    await session.execute(
        update(Wallet)
        .where(Wallet.user_id == user_id, Wallet.status == "active")
        .values(status="replaced", replaced_at=func.now())
    )
    wallet = Wallet(
        user_id=user_id,
        chain="BEP20",
        address_encrypted=address_encrypted,
        address_hash=address_hash,
        address_masked=address_masked,
        status="active",
    )
    session.add(wallet)
    await session.flush()
    return wallet


async def add_account(
    session: AsyncSession, user_id: int, exchange: str, uid: str, accrual_since: date
) -> ExchangeAccount | None:
    existing = await session.scalar(
        select(ExchangeAccount).where(
            ExchangeAccount.exchange == exchange, ExchangeAccount.uid == uid
        )
    )
    if existing is not None:
        if existing.user_id != user_id:
            return None  # first-come: held by ANOTHER user (active or archived) -> collision
        # Same owner re-adding their own UID: resurrect an archived one (reversible
        # unbind — keeps its accrual_since so the gap backfills within the 30-day
        # window); an already-active one is returned unchanged (no duplicate row).
        if existing.status != "active":
            existing.status = "active"
            await session.flush()
        return existing
    # Use a SAVEPOINT (begin_nested) rather than a raw session.rollback() on IntegrityError:
    # under the test fixture's own savepoint-based transaction (db_session in
    # tests/botui/conftest.py uses join_transaction_mode="create_savepoint"), a bare
    # session.rollback() rolls back to the enclosing savepoint and would tear down the whole
    # test's transaction, not just this insert attempt. Nesting our own SAVEPOINT keeps the
    # rollback scoped to just this insert, so a genuine (exchange, uid) race still returns
    # None instead of raising or corrupting the caller's transaction. Per SQLAlchemy docs
    # (session_transaction.html "Using SAVEPOINT"), add() belongs inside the begin_nested()
    # block with no separate manual flush; exiting the block cleanly issues the flush.
    account = ExchangeAccount(
        user_id=user_id, exchange=exchange, uid=uid, status="active", accrual_since=accrual_since
    )
    try:
        async with session.begin_nested():
            session.add(account)
    except IntegrityError:
        return None  # race: someone claimed it between check and insert
    return account


async def list_accounts(session: AsyncSession, user_id: int) -> list[ExchangeAccount]:
    stmt = (
        select(ExchangeAccount)
        .where(ExchangeAccount.user_id == user_id, ExchangeAccount.status == "active")
        .order_by(ExchangeAccount.id)
    )
    return list((await session.scalars(stmt)).all())


async def uid_holder(session: AsyncSession, exchange: str, uid: str) -> tuple[int, str] | None:
    """``(user_id, status)`` of whoever currently holds ``(exchange, uid)``, or None.

    Used to attribute a first-come collision so the owner can be alerted and decide a
    reassignment."""
    row = (
        await session.execute(
            select(ExchangeAccount.user_id, ExchangeAccount.status).where(
                ExchangeAccount.exchange == exchange, ExchangeAccount.uid == uid
            )
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


async def archive_account(session: AsyncSession, user_id: int, account_id: int) -> bool:
    result = cast(
        "CursorResult[None]",
        await session.execute(
            update(ExchangeAccount)
            .where(
                ExchangeAccount.id == account_id,
                ExchangeAccount.user_id == user_id,
                ExchangeAccount.status == "active",
            )
            .values(status="archived")
        ),
    )
    return (result.rowcount or 0) > 0


async def available_accruals(
    session: AsyncSession, user_id: int, asset: str | None = None
) -> list[RebateAccrual]:
    """Live-and-withdrawable accruals for ``user_id``, oldest first (FIFO).

    ``asset`` optionally restricts to a single asset pool (e.g. ``"USDT"``/``"MNT"``);
    ``None`` (default) returns every asset, preserving the prior all-assets behaviour."""
    conditions = [RebateAccrual.user_id == user_id, RebateAccrual.state == "available"]
    if asset is not None:
        conditions.append(RebateAccrual.asset == asset)
    stmt = select(RebateAccrual).where(*conditions).order_by(RebateAccrual.accrual_date)
    return list((await session.scalars(stmt)).all())


async def account_month_net(
    session: AsyncSession, account_id: int, start: date, before: date
) -> Decimal:
    """Sum of a single account's accrued NET (after the platform fee) for
    ``[start, before)`` — any state. NET, not gross: the per-UID figure shown to the
    user must match "доступно к выводу" and never reveal the platform's cut."""
    stmt = select(func.coalesce(func.sum(RebateAccrual.net_usdt), 0)).where(
        RebateAccrual.account_id == account_id,
        RebateAccrual.accrual_date >= start,
        RebateAccrual.accrual_date < before,
    )
    return Decimal(await session.scalar(stmt) or 0)


async def account_month_by_asset(
    session: AsyncSession, account_id: int, start: date, before: date
) -> dict[str, tuple[Decimal, Decimal]]:
    """Per-asset accrued NET for a single account over ``[start, before)`` (any state),
    as ``{asset: (net_native, net_usdt)}``. ``net_native`` is the real coin amount (e.g.
    MNT for the MNT pool); ``net_usdt`` is its USDT-equivalent. Lets the stats screen
    show a non-USDT rebate in its own coin with the USDT value in parentheses."""
    stmt = (
        select(
            RebateAccrual.asset,
            func.coalesce(func.sum(RebateAccrual.net_native), 0),
            func.coalesce(func.sum(RebateAccrual.net_usdt), 0),
        )
        .where(
            RebateAccrual.account_id == account_id,
            RebateAccrual.accrual_date >= start,
            RebateAccrual.accrual_date < before,
        )
        .group_by(RebateAccrual.asset)
    )
    rows = (await session.execute(stmt)).all()
    return {str(asset): (Decimal(nn or 0), Decimal(nu or 0)) for asset, nn, nu in rows}


async def last_success_withdrawal_at(
    session: AsyncSession, user_id: int, asset: str | None = None
) -> datetime | None:
    """Most recent successful withdrawal timestamp (confirmed_at, falling back to sent_at).

    ``asset`` optionally restricts to one pool (``"USDT"``/``"MNT"``) so the withdrawal
    cooldown can be computed independently per asset; ``None`` (default) spans every
    asset. Legacy payouts predate the ``asset`` column but were backfilled to ``"USDT"``
    by migration 536cf94b2929, so ``asset="USDT"`` still counts them."""
    conditions = [
        Payout.user_id == user_id,
        Payout.status.in_(_SUCCESS_STATUSES),
        Payout.purpose == "rebate",
    ]
    if asset is not None:
        conditions.append(Payout.asset == asset)
    stmt = select(func.max(func.coalesce(Payout.confirmed_at, Payout.sent_at))).where(*conditions)
    return cast("datetime | None", await session.scalar(stmt))


async def has_active_reserved_payout(session: AsyncSession, user_id: int) -> bool:
    """True iff the user has a payout currently sitting in ``reserved``."""
    stmt = select(
        select(Payout.id).where(Payout.user_id == user_id, Payout.status == "reserved").exists()
    )
    return bool(await session.scalar(stmt))


_ACTIVE_PAYOUT_STATUSES = ("reserved", "pending_admin", "submitting", "sent")


async def has_active_payout(session: AsyncSession, user_id: int, asset: str | None = None) -> bool:
    """True iff the user has a payout in any non-terminal state.

    Broader than ``has_active_reserved_payout``: covers ``reserved``,
    ``pending_admin``, ``submitting`` and ``sent``.

    ``asset`` scopes the check to ONE pool (``"USDT"``/``"MNT"``), enforcing one active
    withdrawal *per asset* — a pending USDT payout blocks only another USDT withdrawal,
    not an MNT one, and vice versa. ``None`` spans both pools (any-active). Nonce safety
    is NOT this gate's job: the executor serializes every on-chain send globally
    (``payouts_in_flight``), so two coexisting reserved payouts (one USDT + one MNT) are
    still broadcast one at a time."""
    conditions = [Payout.user_id == user_id, Payout.status.in_(_ACTIVE_PAYOUT_STATUSES)]
    if asset is not None:
        conditions.append(Payout.asset == asset)
    stmt = select(select(Payout.id).where(*conditions).exists())
    return bool(await session.scalar(stmt))


async def create_action_token(
    session: AsyncSession,
    *,
    token: uuid.UUID,
    user_id: int,
    action_type: str,
    payload: dict[str, Any],
    expires_at: datetime,
) -> None:
    session.add(
        ActionToken(
            token=token,
            user_id=user_id,
            action_type=action_type,
            payload=payload,
            state="issued",
            expires_at=expires_at,
        )
    )
    await session.flush()


async def consume_action_token(
    session: AsyncSession,
    token: uuid.UUID,
    now: datetime,
    *,
    user_id: int | None = None,
    action_type: str | None = None,
) -> dict[str, Any] | None:
    """Atomically mark ``token`` used and return its payload for the caller that wins the race.

    SECURITY-CRITICAL: single UPDATE ... WHERE state='issued' AND expires_at > now ... RETURNING
    payload. This is the anti-double-spend primitive: a SELECT-then-UPDATE would reintroduce the
    race this function exists to close.
    """
    conditions = [
        ActionToken.token == token,
        ActionToken.state == "issued",
        ActionToken.expires_at > now,
    ]
    if user_id is not None:
        conditions.append(ActionToken.user_id == user_id)
    if action_type is not None:
        conditions.append(ActionToken.action_type == action_type)
    result = await session.execute(
        update(ActionToken)
        .where(*conditions)
        .values(state="used", used_at=now)
        .returning(ActionToken.payload)
    )
    row = result.first()
    if row is None:
        return None
    return cast("dict[str, Any] | None", row[0])


async def release_action_token(session: AsyncSession, token: uuid.UUID) -> None:
    """Revert `token` from `used` back to `issued` (clearing `used_at`).

    Used by ``confirm_withdrawal`` when, having already consumed the token, it
    discovers a concurrent confirm (a different token, e.g. the other asset pool)
    already created an active payout for this user. That confirm must be refused,
    but this token was a legitimate single-use grant that was never actually
    fulfilled — reverting it lets a later retry still spend it once the other
    request resolves, instead of wasting a token the user never got to use.
    """
    await session.execute(
        update(ActionToken)
        .where(ActionToken.token == token, ActionToken.state == "used")
        .values(state="issued", used_at=None)
    )


async def reserve_accruals_and_create_payout(
    session: AsyncSession,
    user_id: int,
    accrual_ids: list[int],
    *,
    wallet: Wallet,
    gross: Decimal,
    commission: Decimal,
    net: Decimal,
    idempotency_key: str,
    asset: str = "USDT",
    net_native: Decimal = Decimal("0"),
    initial_status: str = "reserved",
) -> Payout | None:
    """Atomically move ``accrual_ids`` available->reserved and open a Payout.

    SECURITY-CRITICAL (money): locks the named rows FOR UPDATE and re-verifies every
    one is still 'available' before creating anything. If the set drifted (some row
    already reserved/expired/paid since the caller computed the preview) this returns
    None and creates nothing — no partial payout, no partial reservation.

    ``asset``/``net_native`` record which pool this payout pays out and its native
    (non-USDT-equiv) amount; ``initial_status`` lets the caller open MNT payouts
    directly in ``pending_admin`` (MNT is always admin-gated) while USDT keeps
    opening ``reserved`` (the executor separately routes large USDT to
    ``pending_admin``).
    """
    locked_stmt = (
        select(RebateAccrual)
        .where(
            RebateAccrual.id.in_(accrual_ids),
            RebateAccrual.state == "available",
            RebateAccrual.user_id == user_id,
            RebateAccrual.asset == asset,
        )
        .with_for_update()
    )
    locked = list((await session.scalars(locked_stmt)).all())
    if len(locked) != len(accrual_ids):
        return None  # drift: something expired/reserved/paid since the preview was issued

    covered_from = min(a.accrual_date for a in locked)
    covered_to = max(a.accrual_date for a in locked)

    payout = Payout(
        user_id=user_id,
        wallet_id=wallet.id,
        address_encrypted=wallet.address_encrypted,
        address_masked=wallet.address_masked,
        covered_from=covered_from,
        covered_to=covered_to,
        gross_usdt=gross,
        commission_usdt=commission,
        net_usdt=net,
        asset=asset,
        net_native=net_native,
        status=initial_status,
        idempotency_key=idempotency_key,
    )
    session.add(payout)
    await session.flush()  # assigns payout.id

    result = cast(
        "CursorResult[None]",
        await session.execute(
            update(RebateAccrual)
            .where(RebateAccrual.id.in_(accrual_ids), RebateAccrual.state == "available")
            .values(state="reserved", payout_id=payout.id)
        ),
    )
    if (result.rowcount or 0) != len(accrual_ids):
        # Guaranteed not to happen given the FOR UPDATE lock above; if it ever does,
        # force a rollback rather than leave a partially-reserved set.
        raise RuntimeError("accrual reservation drifted after lock; rolling back")
    return payout


async def write_audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            new_value=new_value,
        )
    )
    await session.flush()
