"""Partner ranks: each threshold is an alternative, never a cumulative gate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

THRESHOLDS = (50, 200, 1000, 10000)


@dataclass(frozen=True)
class PartnerRank:
    level: int
    share: Decimal
    counts: tuple[int | None, int | None, int | None, int | None]


RANKS = (
    PartnerRank(1, Decimal("0.20"), (3, None, None, None)),
    PartnerRank(2, Decimal("0.25"), (7, None, None, None)),
    PartnerRank(3, Decimal("0.30"), (10, 3, None, None)),
    PartnerRank(4, Decimal("0.35"), (20, 7, None, None)),
    PartnerRank(5, Decimal("0.40"), (50, 15, 5, None)),
    PartnerRank(6, Decimal("0.45"), (80, 20, 8, None)),
    PartnerRank(7, Decimal("0.50"), (100, 25, 10, 1)),
)
ZERO_RANK = PartnerRank(0, Decimal(0), (None, None, None, None))


def rank_by_level(level: int) -> PartnerRank:
    if level == 0:
        return ZERO_RANK
    if not 1 <= level <= len(RANKS):
        raise ValueError("Unknown partner rank.")
    return RANKS[level - 1]


def rank_for_counts(counts: tuple[int, int, int, int]) -> PartnerRank:
    if any(n < 0 for n in counts):
        raise ValueError("Qualification counts cannot be negative.")
    for rank in reversed(RANKS):
        if any(
            required is not None and actual >= required
            for actual, required in zip(counts, rank.counts, strict=True)
        ):
            return rank
    return ZERO_RANK
