# bot/botui/service.py
"""aiogram-free application logic for the cabinet. Decimal money throughout."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.accrual import repository as acc_repo
from bot.admin import repository as admin_repo
from bot.botui import repository as repo
from bot.config import SPEC_BY_CODE, get_settings
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.payouts import Payout
from bot.exchanges.base import ReferralCheck
from bot.i18n import gettext as tr
from bot.payout.expiry import next_withdrawal_allowed
from bot.payout.rank_timing import month_start, next_month_start, prev_month_start
from bot.payout.ranks import effective_rank, fee_pct, is_payable, rank_for_gross
from bot.security.secrets import SecretCipher

# (exchange_code, uid) -> membership verdict. Injected so the DB-only service stays
# testable without network; the aiogram handler passes a real network-backed one.
ReferralVerifier = Callable[[str, str], Awaitable[ReferralCheck]]

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_UID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")

MIN_PAYOUT_USDT = Decimal("10")  # reused by Task 7 (withdraw flow)


class AddressError(ValueError):
    """The submitted wallet address is not a valid BEP20 (EVM) address."""


class UidError(ValueError):
    """Malformed exchange code or UID."""


class AssetError(ValueError):
    """Unsupported withdrawal asset (must be one of ``WITHDRAW_ASSETS``)."""


WITHDRAW_ASSETS = frozenset({"USDT", "MNT"})


def validate_address(raw: str) -> str:
    addr = raw.strip()
    if not _ADDRESS_RE.match(addr):
        raise AddressError(tr("Адрес должен быть в формате 0x + 40 hex-символов."))
    return addr.lower()


def mask_address(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def address_hash(addr: str) -> bytes:
    return hashlib.sha256(addr.lower().encode("utf-8")).digest()


async def set_wallet(session: AsyncSession, cipher: SecretCipher, user_id: int, raw: str) -> str:
    addr = validate_address(raw)
    encrypted = cipher.encrypt(addr).encode("ascii")
    masked = mask_address(addr)
    await repo.replace_wallet(
        session,
        user_id,
        address_encrypted=encrypted,
        address_hash=address_hash(addr),
        address_masked=masked,
    )
    await repo.write_audit(
        session,
        actor=str(user_id),
        action="wallet_replaced",
        entity_type="wallet",
        entity_id=str(user_id),
        new_value={"masked": masked},
    )
    return masked


@dataclass(slots=True)
class AddUidResult:
    """Outcome of an add-UID attempt.

    ``outcome`` is one of ``"added"`` / ``"collision"`` / ``"not_referral"``.
    ``status`` is the membership verdict (also meaningful for ``"added"``: it says
    whether ownership was confirmed or only added unconfirmed).
    """

    outcome: str
    status: ReferralCheck
    account: ExchangeAccount | None = None
    held_by: int | None = None  # on "collision": the user_id currently holding the uid


async def add_uid(
    session: AsyncSession,
    user_id: int,
    exchange: str,
    uid: str,
    epoch: date,
    *,
    verifier: ReferralVerifier | None = None,
) -> AddUidResult:
    """Validate, verify referral membership, then persist the UID.

    A ``NOT_REFERRAL`` verdict rejects the add (the uid is authoritatively not the
    owner's referral on this exchange). ``CONFIRMED`` / ``UNVERIFIABLE`` both proceed
    — the latter is added with an "ownership unconfirmed" note (fail-open, because a
    stranger's uid earns nothing anyway and we must not block on a weak API)."""
    if exchange not in SPEC_BY_CODE:
        raise UidError(tr("Неизвестная биржа."))
    uid = uid.strip()
    if not _UID_RE.match(uid):
        raise UidError(tr("UID: только латиница и цифры, до 64 символов."))

    status = ReferralCheck.UNVERIFIABLE
    if verifier is not None:
        status = await verifier(exchange, uid)
    if status is ReferralCheck.NOT_REFERRAL:
        await repo.write_audit(
            session,
            actor=str(user_id),
            action="uid_rejected_not_referral",
            entity_type="exchange_account",
            entity_id=f"{exchange}:{uid}",
        )
        return AddUidResult("not_referral", status)

    account = await repo.add_account(session, user_id, exchange, uid, epoch)
    if account is None:
        holder = await repo.uid_holder(session, exchange, uid)
        await repo.write_audit(
            session,
            actor=str(user_id),
            action="uid_collision",
            entity_type="exchange_account",
            entity_id=f"{exchange}:{uid}",
            new_value={"held_by": holder[0]} if holder else None,
        )
        return AddUidResult("collision", status, held_by=holder[0] if holder else None)
    await repo.write_audit(
        session,
        actor=str(user_id),
        action="uid_added",
        entity_type="exchange_account",
        entity_id=str(account.id),
        new_value={"referral": status.value},
    )
    return AddUidResult("added", status, account)


async def effective_rank_now(session: AsyncSession, user_id: int, today: date) -> int:
    """Current effective rank: this month's tier floored by the last two closed months."""
    m = month_start(today)
    # Bounds by the WHOLE current month (m .. next_month_start(m)); this equals true
    # month-to-date in practice only because the accrual worker never writes future-dated rows.
    mtd = await acc_repo.user_month_gross(session, user_id, m, next_month_start(m))
    achieved_curr = rank_for_gross(mtd)
    carry1 = await acc_repo.get_achieved_rank(session, user_id, prev_month_start(m)) or 0
    carry2 = (
        await acc_repo.get_achieved_rank(session, user_id, prev_month_start(prev_month_start(m)))
        or 0
    )
    return effective_rank(achieved_curr, carry1, carry2)


@dataclass(slots=True)
class BalanceSummary:
    available_gross: Decimal
    available_net: Decimal
    commission: Decimal
    effective_rank: int
    fee_pct: Decimal
    next_withdrawal_at: datetime | None
    nearest_expiry: datetime | None
    withdrawable: bool
    mnt_native: Decimal = Decimal(0)
    mnt_usdt: Decimal = Decimal(0)
    mnt_withdrawable: bool = False
    # Most recent SUCCESSFUL withdrawal (confirmed_at/sent_at of any asset), or None if
    # the user has never had one. Global across pools — an informational "last payout"
    # line, NOT a gate (the cooldown gates are per-asset, see next_withdrawal_at below).
    last_withdrawal_at: datetime | None = None
    # `next_withdrawal_at` is the USDT pool's cooldown end; `mnt_next_withdrawal_at` is
    # the MNT pool's. They are INDEPENDENT — withdrawing one asset never delays the other.
    mnt_next_withdrawal_at: datetime | None = None


async def balance_summary(
    session: AsyncSession, user_id: int, now: datetime, today: date
) -> BalanceSummary:
    """Self-service balance view. Money comes from the FROZEN per-accrual fields
    (commission_usdt/net_usdt/amount_usdt) — never recomputed here.

    The USDT and MNT pools are kept separate: ``available_gross``/``available_net``/
    ``commission`` are USDT-only (unchanged meaning), while ``mnt_native``/``mnt_usdt``
    summarize the MNT pool (USDT-equivalent + native amount) alongside its own
    ``mnt_withdrawable`` flag. Rank/fee/nearest-expiry stay global, but the withdrawal
    COOLDOWN and the one-active-payout limit are computed INDEPENDENTLY per asset —
    withdrawing USDT never delays MNT and vice versa (a user's rank isn't split by
    currency, but each pool's payout timeline is)."""
    accruals = await repo.available_accruals(session, user_id)
    usdt_accruals = await repo.available_accruals(session, user_id, asset="USDT")
    mnt_accruals = await repo.available_accruals(session, user_id, asset="MNT")
    gross = sum((a.amount_usdt for a in usdt_accruals), Decimal(0))
    net = sum((a.net_usdt for a in usdt_accruals), Decimal(0))
    commission = sum((a.commission_usdt for a in usdt_accruals), Decimal(0))
    mnt_usdt = sum((a.net_usdt for a in mnt_accruals), Decimal(0))
    mnt_native = sum((a.net_native for a in mnt_accruals), Decimal(0))
    rank = await effective_rank_now(session, user_id, today)
    payable = is_payable(rank)
    fee = fee_pct(rank) if payable else Decimal(0)
    # Per-asset cooldowns: each pool's next-withdrawal is driven by the last successful
    # payout of THAT asset only. Rank (hence cooldown length) is global.
    last_usdt = await repo.last_success_withdrawal_at(session, user_id, asset="USDT")
    last_mnt = await repo.last_success_withdrawal_at(session, user_id, asset="MNT")
    next_usdt = next_withdrawal_allowed(last_usdt, rank) if payable else None
    next_mnt = next_withdrawal_allowed(last_mnt, rank) if payable else None
    nearest = min((a.expires_at for a in accruals), default=None)
    # Per-asset one-active-payout gate — SAME gate `prepare_withdrawal` enforces
    # (reserved/pending_admin/submitting/sent), scoped to each pool so the balance
    # screen never shows withdrawable=True while a real attempt would be rejected.
    active_usdt = await repo.has_active_payout(session, user_id, asset="USDT")
    active_mnt = await repo.has_active_payout(session, user_id, asset="MNT")
    usdt_cooldown_ok = next_usdt is None or now >= next_usdt
    mnt_cooldown_ok = next_mnt is None or now >= next_mnt
    withdrawable = payable and net >= MIN_PAYOUT_USDT and usdt_cooldown_ok and not active_usdt
    mnt_withdrawable = (
        payable and mnt_usdt >= MIN_PAYOUT_USDT and mnt_cooldown_ok and not active_mnt
    )
    # Informational "last payout" line spans both pools: the most recent of the two.
    last_seen = [d for d in (last_usdt, last_mnt) if d is not None]
    last_any = max(last_seen) if last_seen else None
    return BalanceSummary(
        gross,
        net,
        commission,
        rank,
        fee,
        next_usdt,
        nearest,
        withdrawable,
        mnt_native=mnt_native,
        mnt_usdt=mnt_usdt,
        mnt_withdrawable=mnt_withdrawable,
        last_withdrawal_at=last_any,
        mnt_next_withdrawal_at=next_mnt,
    )


async def last_withdrawal_at(
    session: AsyncSession, user_id: int, asset: str | None = None
) -> datetime | None:
    """Most recent successful withdrawal, or None if there was never one.

    ``asset`` scopes to one pool (``"USDT"``/``"MNT"``) so the withdraw screen can show
    the last payout of the asset being withdrawn; ``None`` spans both. Thin pass-through
    so the aiogram handler need not reach into the repository layer directly."""
    return await repo.last_success_withdrawal_at(session, user_id, asset=asset)


WITHDRAW_TOKEN_TTL = timedelta(minutes=5)


@dataclass(slots=True)
class WithdrawPreview:
    accrual_ids: list[int]
    gross: Decimal
    commission: Decimal
    net: Decimal
    wallet_masked: str
    asset: str = "USDT"
    # For USDT, native == the USDT-equiv `net` above. For MNT, `net` is the
    # USDT-equiv (Σ net_usdt, used for the >= MIN_PAYOUT_USDT threshold) while
    # `net_native` is Σ net_native — the actual MNT amount to pay out.
    net_native: Decimal = Decimal("0")


@dataclass(slots=True)
class WithdrawBlocked:
    # "paused" | "blocked" | "no_wallet" | "rank0" | "pending" | "cooldown" |
    # "below_min" | "mnt_disabled"
    reason: str
    detail: str | None = None  # e.g. cooldown-until ISO for "cooldown"


async def prepare_withdrawal(
    session: AsyncSession, user_id: int, now: datetime, today: date, asset: str = "USDT"
) -> WithdrawPreview | WithdrawBlocked:
    """Eligibility + FIFO preview for a withdrawal in a single asset pool. Money
    comes from the FROZEN per-accrual fields — never recomputed here.

    Kill-switch and blocked-user checks come first (unbypassable, ahead of every
    other eligibility check): a paused payouts flag or a blocked account must
    stop a withdrawal before any balance/eligibility computation runs.

    The "pending" check (``has_active_payout``) and the cooldown are BOTH scoped to
    ``asset``: a pending USDT payout / a recent USDT withdrawal blocks only another
    USDT withdrawal, never an MNT one, and vice versa. The two pools are independent."""
    if asset not in WITHDRAW_ASSETS:
        raise AssetError(f"Unsupported withdrawal asset: {asset!r}")
    # FIX M3: MNT withdraw is feature-flagged on settings.mnt_enabled (MANTLE_RPC_URL).
    # If the flag is removed after MNT was live, a residual MNT balance must not be
    # allowed to create a payout that would only fail closed later in the executor
    # (mnt_disabled) — refuse it here, before touching any balance/eligibility state.
    if asset == "MNT" and not get_settings().mnt_enabled:
        return WithdrawBlocked("mnt_disabled")
    if await admin_repo.is_payouts_paused(session):
        return WithdrawBlocked("paused")
    if (await repo.get_user_status(session, user_id)) == "blocked":
        return WithdrawBlocked("blocked")
    wallet = await repo.get_active_wallet(session, user_id)
    if wallet is None:
        return WithdrawBlocked("no_wallet")
    accruals = await repo.available_accruals(session, user_id, asset=asset)  # FIFO
    gross = sum((a.amount_usdt for a in accruals), Decimal(0))
    commission = sum((a.commission_usdt for a in accruals), Decimal(0))
    net = sum((a.net_usdt for a in accruals), Decimal(0))  # USDT-equiv, both assets
    net_native = sum((a.net_native for a in accruals), Decimal(0))
    rank = await effective_rank_now(session, user_id, today)
    if not is_payable(rank):
        return WithdrawBlocked("rank0")
    if await repo.has_active_payout(session, user_id, asset=asset):
        return WithdrawBlocked("pending")
    last = await repo.last_success_withdrawal_at(session, user_id, asset=asset)
    next_at = next_withdrawal_allowed(last, rank)
    if next_at is not None and now < next_at:
        return WithdrawBlocked("cooldown", next_at.isoformat())
    if net < MIN_PAYOUT_USDT:
        return WithdrawBlocked("below_min")
    return WithdrawPreview(
        [a.id for a in accruals],
        gross,
        commission,
        net,
        wallet.address_masked,
        asset=asset,
        net_native=net_native,
    )


async def issue_withdraw_token(
    session: AsyncSession, user_id: int, preview: WithdrawPreview, now: datetime
) -> uuid.UUID:
    token = uuid.uuid4()
    await repo.create_action_token(
        session,
        token=token,
        user_id=user_id,
        action_type="withdraw",
        payload={
            "accrual_ids": preview.accrual_ids,
            "gross": str(preview.gross),
            "commission": str(preview.commission),
            "net": str(preview.net),
            "asset": preview.asset,
            "net_native": str(preview.net_native),
        },
        expires_at=now + WITHDRAW_TOKEN_TTL,
    )
    return token


async def confirm_withdrawal(
    session: AsyncSession, user_id: int, token: uuid.UUID, now: datetime
) -> Payout | WithdrawBlocked | None:
    """Single-use confirm: the action token is the anti-double-spend gate — a second
    call with the same token loses the atomic consume race and returns None here
    before ever touching accruals.

    Re-asserts the kill-switch and blocked-user gates FIRST (before consuming the
    token): ``prepare_withdrawal`` checked them, but the token lives for
    ``WITHDRAW_TOKEN_TTL`` and an admin can pause payouts or block the user inside
    that window. Checking here (and leaving the token unconsumed on a block, so a
    later legitimate retry still works) stops a blocked/paused user from creating a
    reserved payout the executor would otherwise send.

    Also re-checks ``has_active_payout`` here, scoped to the token's asset: the "one
    active withdrawal PER asset" invariant is otherwise only enforced at
    ``prepare_withdrawal`` (token issue) time, so a user who calls prepare twice within
    the token TTL for the SAME asset would hold two live tokens and could confirm both,
    ending with two simultaneously-active same-asset payouts. Two DIFFERENT assets
    (one USDT + one MNT) confirming within the window is allowed by design — the pools
    are independent.

    This check runs AFTER ``consume_action_token`` (not before) so the token's own
    single-use gate — the thing that makes a same-token double-confirm return
    ``None`` here, unchanged — stays the sole authority over THAT race. If the
    active-payout check then trips (a genuinely different, still-valid same-asset token
    whose payout would collide with one a concurrent confirm already reserved), the just
    -consumed token is reverted back to ``issued`` via ``release_action_token``
    before returning ``WithdrawBlocked("pending")`` — net effect: this token was
    never really spent, so a legitimate retry still works once the other request
    resolves."""
    if await admin_repo.is_payouts_paused(session):
        return WithdrawBlocked("paused")
    if (await repo.get_user_status(session, user_id)) == "blocked":
        return WithdrawBlocked("blocked")
    # Both rebate and partner confirmations serialize on the same owner row.
    await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
    if (await repo.get_user_status(session, user_id)) == "blocked":
        return WithdrawBlocked("blocked")
    payload = await repo.consume_action_token(
        session, token, now, user_id=user_id, action_type="withdraw"
    )
    if payload is None:
        return None  # expired / already used
    asset = str(payload.get("asset", "USDT"))
    net_native = Decimal(str(payload.get("net_native", "0")))
    # Per-asset re-check: two live tokens (one per asset) issued within the TTL may BOTH
    # confirm — that is intended (one active USDT + one active MNT payout can coexist).
    # Only a second payout of the SAME asset is blocked here.
    if await repo.has_active_payout(session, user_id, asset=asset):
        await repo.release_action_token(session, token)
        return WithdrawBlocked("pending")
    wallet = await repo.get_active_wallet(session, user_id)
    if wallet is None:
        return None
    # MNT is ALWAYS admin-gated (no auto-send path exists for it); USDT stays
    # "reserved" — the executor separately routes large USDT amounts to
    # pending_admin via its own auto-limit check.
    initial_status = "pending_admin" if asset == "MNT" else "reserved"
    payout = await repo.reserve_accruals_and_create_payout(
        session,
        user_id,
        [int(i) for i in payload["accrual_ids"]],
        wallet=wallet,
        gross=Decimal(payload["gross"]),
        commission=Decimal(payload["commission"]),
        net=Decimal(payload["net"]),
        idempotency_key=f"wd:{user_id}:{token}",
        asset=asset,
        net_native=net_native,
        initial_status=initial_status,
    )
    if payout is not None:
        await repo.write_audit(
            session,
            actor=str(user_id),
            action="withdraw_reserved",
            entity_type="payout",
            entity_id=str(payout.id),
        )
    return payout
