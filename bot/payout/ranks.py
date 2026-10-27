"""Rank classification, fees and withdrawal cooldowns.

Authoritative rules: PAYOUT_BUSINESS_LOGIC.md §4-5. Pure functions over Decimal.
Rank 0 (< $10 monthly gross) is non-payable and is intentionally absent from the
FEE_PCT / COOLDOWN_DAYS tables.
"""

from __future__ import annotations

from decimal import Decimal

from bot.i18n import gettext as tr

# fee (комса) withheld by the bot, per payable rank.
FEE_PCT: dict[int, Decimal] = {
    1: Decimal("0.10"),
    2: Decimal("0.07"),
    3: Decimal("0.03"),
    4: Decimal("0"),
}

# minimum days between withdrawals, per payable rank.
COOLDOWN_DAYS: dict[int, int] = {1: 30, 2: 14, 3: 7, 4: 1}

# upper bounds (inclusive) of monthly gross USDT for ranks 1-3; above => rank 4.
_R1_MAX = Decimal("100")
_R2_MAX = Decimal("1000")
_R3_MAX = Decimal("10000")
_R1_MIN = Decimal("10")


def rank_for_gross(gross_usdt: Decimal) -> int:
    """Map monthly gross rebate (USDT) to rank 0-4."""
    if gross_usdt < _R1_MIN:
        return 0
    if gross_usdt <= _R1_MAX:
        return 1
    if gross_usdt <= _R2_MAX:
        return 2
    if gross_usdt <= _R3_MAX:
        return 3
    return 4


def rank_label(rank: int) -> str:
    """User-facing tier name. Ranks are branded "Манки N" (0-4); rank 0 is the
    non-payable floor. Single source of truth for how a rank is shown to users."""
    return tr("Манки {v0}").format(v0=rank)


def is_payable(rank: int) -> bool:
    """True for ranks that can withdraw (1-4); False for rank 0."""
    return rank >= 1


def fee_pct(rank: int) -> Decimal:
    """Fee fraction for a payable rank. Raises ValueError for rank 0/unknown."""
    try:
        return FEE_PCT[rank]
    except KeyError:
        raise ValueError(f"no fee for rank {rank}") from None


def cooldown_days(rank: int) -> int:
    """Cooldown days for a payable rank. Raises ValueError for rank 0/unknown."""
    try:
        return COOLDOWN_DAYS[rank]
    except KeyError:
        raise ValueError(f"no cooldown for rank {rank}") from None


def effective_rank(achieved_current: int, achieved_prev1: int, achieved_prev2: int) -> int:
    """Effective rank = highest rank reached this month or in the previous two.

    A rank earned in month M protects M+1 and M+2 as a floor; upgrades apply
    immediately. Missing history is passed as 0. See PAYOUT_BUSINESS_LOGIC.md §4.3.
    """
    return max(achieved_current, achieved_prev1, achieved_prev2)
