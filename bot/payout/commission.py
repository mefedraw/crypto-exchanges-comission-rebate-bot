"""Per-tranche commission and net. Commission is frozen at accrual-time rank and
summed; never recomputed retroactively. See PAYOUT_BUSINESS_LOGIC.md §5.2.
"""

from __future__ import annotations

from decimal import Decimal

from bot.payout.ranks import fee_pct, is_payable

Tranche = tuple[Decimal, int]  # (amount_usdt, rank_at_accrual)


def commission_for(amount: Decimal, rank: int) -> Decimal:
    """Bot fee withheld from one tranche at its accrual-time rank (1-4)."""
    return amount * fee_pct(rank)


def net_for(amount: Decimal, rank: int) -> Decimal:
    """User's share of one tranche (before gas), rank 1-4."""
    return amount - commission_for(amount, rank)


def effective_fee(rank: int, override_pct: Decimal | None) -> Decimal:
    """Frozen fee for a payable rank, lowered by an optional per-user override.

    Only-down: the override can reduce the fee (user gets more) but never raise it
    above the rank fee. Caller guarantees rank >= 1. See admin design (B)."""
    base = fee_pct(rank)
    if override_pct is None:
        return base
    return min(base, override_pct)


def totals(tranches: list[Tranche]) -> tuple[Decimal, Decimal, Decimal]:
    """Sum (gross, commission, net) over payable tranches. Rank-0 tranches are
    non-payable and excluded entirely."""
    gross = Decimal(0)
    commission = Decimal(0)
    net = Decimal(0)
    for amount, rank in tranches:
        if not is_payable(rank):
            continue
        c = commission_for(amount, rank)
        gross += amount
        commission += c
        net += amount - c
    return gross, commission, net
