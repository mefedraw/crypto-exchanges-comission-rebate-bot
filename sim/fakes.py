"""Deterministic fake exchange adapters + per-user earning profiles.

The accrual worker calls ``adapter.get_commission(uid, day_start, day_end)`` once
per closed day. Here every adapter is backed by a shared ``uid -> Profile`` map and
returns a fully deterministic amount for a given (uid, day): same seed => same run,
so a simulation is reproducible and its invariants are checkable.

Only the Bybit adapter exposes ``mnt_usdt_rate`` (the worker gates MNT freezing on
that method's presence), and only bybit-hosted uids carry an MNT component — exactly
mirroring production, where Bybit is the sole MNT-bearing exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from random import Random

from bot.exchanges.base import (
    CommissionResult,
    ExchangeAdapter,
    ExchangeUnavailableError,
    ReferralCheck,
)

# Fixed MNT->USDT rate for the sim (production pulls the live Bybit MNTUSDT ticker).
MNT_USDT_RATE = Decimal("0.60")


@dataclass
class FailureFlags:
    """Mutable toggles the harness flips on a given sim-day to inject failures.

    ``fail_uid``: when an adapter is asked for this uid, it raises (simulates a per-user
    exchange/key outage). ``fail_mnt_rate``: the Bybit adapter's ``mnt_usdt_rate`` raises
    (simulates the MNT price feed being unavailable for a cycle).
    """

    fail_uid: str | None = None
    fail_mnt_rate: bool = False


@dataclass(frozen=True, slots=True)
class Profile:
    """One exchange account's synthetic earning behaviour.

    ``spike_month`` (first-of-month) elevates a single month to ``[spike_lo, spike_hi]``
    daily so a user briefly reaches rank 4 without an unrealistic all-time whale profile.
    """

    seed: int
    usdt_lo: Decimal
    usdt_hi: Decimal
    zero_prob: float = 0.1
    mnt_lo: Decimal = Decimal(0)
    mnt_hi: Decimal = Decimal(0)
    spike_month: date | None = None
    spike_lo: Decimal = Decimal(0)
    spike_hi: Decimal = Decimal(0)

    def _rng(self, d: date) -> Random:
        # Deterministic per (account, day): int-seeded, so no PYTHONHASHSEED effects.
        return Random(self.seed * 1_000_003 + d.toordinal())

    def usdt_for(self, d: date) -> Decimal:
        rng = self._rng(d)
        if rng.random() < self.zero_prob:
            return Decimal(0)
        if self.spike_month is not None and d.replace(day=1) == self.spike_month:
            lo, hi = float(self.spike_lo), float(self.spike_hi)
        else:
            lo, hi = float(self.usdt_lo), float(self.usdt_hi)
        return Decimal(str(round(rng.uniform(lo, hi), 6)))

    def mnt_for(self, d: date) -> Decimal:
        if self.mnt_hi <= 0:
            return Decimal(0)
        # Reuse the same rng stream, advanced once, so MNT correlates with (not
        # equals) the USDT draw and its own zero-days differ.
        rng = self._rng(d)
        rng.random()  # advance past the usdt zero-check draw
        if rng.random() < self.zero_prob:
            return Decimal(0)
        lo, hi = float(self.mnt_lo), float(self.mnt_hi)
        return Decimal(str(round(rng.uniform(lo, hi), 6)))


class FakeAdapter(ExchangeAdapter):
    """A single exchange's adapter, serving every uid from the shared profile map.

    Emits one USDT :class:`CommissionResult` line per earning day. Non-earning days
    return an empty result (the worker simply freezes nothing for that account-day).
    """

    max_window_days = 30
    supports_uid_filter = True
    supports_date_range = True

    def __init__(
        self, code: str, profiles: dict[str, Profile], flags: FailureFlags | None = None
    ) -> None:
        self.code = code
        self.name = code.capitalize()
        self._profiles = profiles
        self._flags = flags or FailureFlags()

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        if self._flags.fail_uid is not None and uid == self._flags.fail_uid:
            raise ExchangeUnavailableError(f"sim-injected outage for {uid}")
        result = self._new_result(uid, date_from, date_to)
        profile = self._profiles.get(uid)
        if profile is None:
            return result.finalize()
        d = date_from.astimezone(date_from.tzinfo).date()
        usdt = profile.usdt_for(d)
        if usdt > 0:
            result.add_amount("USDT", usdt)
            result.raw_records_count += 1
        self._maybe_add_mnt(result, profile, d)
        return result.finalize()

    def _maybe_add_mnt(self, result: CommissionResult, profile: Profile, d: date) -> None:
        """No-op for a plain (non-Bybit) adapter."""

    async def verify_referral(self, uid: str) -> ReferralCheck:
        return ReferralCheck.CONFIRMED if uid in self._profiles else ReferralCheck.UNVERIFIABLE


class FakeBybitAdapter(FakeAdapter):
    """Bybit variant: also emits an MNT line for MNT-bearing uids and exposes the
    ``mnt_usdt_rate`` the worker needs to value MNT in USDT-equivalent."""

    def _maybe_add_mnt(self, result: CommissionResult, profile: Profile, d: date) -> None:
        mnt = profile.mnt_for(d)
        if mnt > 0:
            result.add_amount("MNT", mnt)
            result.raw_records_count += 1

    async def mnt_usdt_rate(self) -> Decimal:
        if self._flags.fail_mnt_rate:
            raise ExchangeUnavailableError("sim-injected MNT rate outage")
        return MNT_USDT_RATE
