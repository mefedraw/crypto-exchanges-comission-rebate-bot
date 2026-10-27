# Payout Domain Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure business-logic core of the rebate payout bot — rank
classification, effective-rank carry-over, per-tranche commission/net, and accrual
availability/expiry/cooldown date math — with no DB, network, or blockchain dependencies.

**Architecture:** A new `bot/payout/` package of pure, side-effect-free functions over
`Decimal` and `datetime`. Everything downstream (accrual-worker, payout-executor, bot UX)
consumes these functions. This plan is phase 1 of several; DB schema, accrual-worker, bot
UX, and EVM executor are separate plans.

**Tech Stack:** Python 3.11+, `decimal.Decimal` (never float), stdlib `datetime`, pytest.

## Global Constraints

- Money is `Decimal` only — never `float`. (CLAUDE.md)
- Full typing; `mypy` strict must pass on `bot/`. (CLAUDE.md)
- `ruff check` and `ruff format --check` must pass. (CLAUDE.md)
- Business rules are authoritative in [PAYOUT_BUSINESS_LOGIC.md](../../../PAYOUT_BUSINESS_LOGIC.md):
  - Ranks by monthly gross USDT: R0 `<10`, R1 `[10,100]`, R2 `(100,1000]`, R3 `(1000,10000]`, R4 `>10000`.
  - Fees: R1 10%, R2 7%, R3 3%, R4 0%. R0 = non-payable.
  - Cooldown (days between withdrawals): R1 30, R2 14, R3 7, R4 1. R0 none.
  - Effective rank = `max(achieved(M), achieved(M-1), achieved(M-2))`.
  - Accrual for day D: available from start of D+1; expires at start of D+30 (+grace). Day boundaries in `PAYOUT_TIMEZONE = UTC+8`.
  - Commission is frozen per tranche at accrual-time rank; summed, never recomputed.

---

### Task 1: Rank classification, fees, cooldown

**Files:**
- Create: `bot/payout/__init__.py` (empty)
- Create: `bot/payout/ranks.py`
- Test: `tests/test_payout_ranks.py`

**Interfaces:**
- Produces:
  - `FEE_PCT: dict[int, Decimal]` — payable ranks 1–4 only.
  - `COOLDOWN_DAYS: dict[int, int]` — payable ranks 1–4 only.
  - `rank_for_gross(gross_usdt: Decimal) -> int` — returns 0–4.
  - `fee_pct(rank: int) -> Decimal` — rank 1–4; raises `ValueError` otherwise.
  - `cooldown_days(rank: int) -> int` — rank 1–4; raises `ValueError` otherwise.
  - `is_payable(rank: int) -> bool` — `rank >= 1`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_payout_ranks.py
from decimal import Decimal

import pytest

from bot.payout.ranks import (
    COOLDOWN_DAYS,
    FEE_PCT,
    cooldown_days,
    fee_pct,
    is_payable,
    rank_for_gross,
)


@pytest.mark.parametrize(
    "gross, rank",
    [
        ("0", 0), ("9.99", 0),
        ("10", 1), ("100", 1),
        ("100.01", 2), ("1000", 2),
        ("1000.01", 3), ("10000", 3),
        ("10000.01", 4), ("999999", 4),
    ],
)
def test_rank_for_gross_boundaries(gross, rank):
    assert rank_for_gross(Decimal(gross)) == rank


def test_fee_pct_values():
    assert FEE_PCT == {
        1: Decimal("0.10"), 2: Decimal("0.07"), 3: Decimal("0.03"), 4: Decimal("0"),
    }
    assert fee_pct(1) == Decimal("0.10")


def test_cooldown_values():
    assert COOLDOWN_DAYS == {1: 30, 2: 14, 3: 7, 4: 1}
    assert cooldown_days(3) == 7


def test_is_payable():
    assert not is_payable(0)
    assert is_payable(1)
    assert is_payable(4)


def test_fee_and_cooldown_reject_rank_zero():
    with pytest.raises(ValueError):
        fee_pct(0)
    with pytest.raises(ValueError):
        cooldown_days(0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_payout_ranks.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.payout'`

- [ ] **Step 3: Write minimal implementation**

```python
# bot/payout/__init__.py
```

```python
# bot/payout/ranks.py
"""Rank classification, fees and withdrawal cooldowns.

Authoritative rules: PAYOUT_BUSINESS_LOGIC.md §4-5. Pure functions over Decimal.
Rank 0 (< $10 monthly gross) is non-payable and is intentionally absent from the
FEE_PCT / COOLDOWN_DAYS tables.
"""

from __future__ import annotations

from decimal import Decimal

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_payout_ranks.py -q`
Expected: PASS (all cases)

- [ ] **Step 5: Commit**

```bash
git add bot/payout/__init__.py bot/payout/ranks.py tests/test_payout_ranks.py
git commit -m "feat(payout): rank classification, fees, cooldown table"
```

---

### Task 2: Effective-rank carry-over

**Files:**
- Modify: `bot/payout/ranks.py` (append `effective_rank`)
- Test: `tests/test_payout_ranks.py` (append)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `effective_rank(achieved_current: int, achieved_prev1: int, achieved_prev2: int) -> int`
  — `max` of the three; missing history passed as `0`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_payout_ranks.py
from bot.payout.ranks import effective_rank


def test_effective_rank_is_max_of_window():
    assert effective_rank(1, 0, 0) == 1
    assert effective_rank(1, 2, 0) == 2      # protection from prev month
    assert effective_rank(3, 1, 2) == 3      # immediate upgrade


def test_effective_rank_business_example_1():
    # achieved months: M1=2, M2=1, M3=3, M4=1, M5=1, M6=1
    assert effective_rank(2, 0, 0) == 2      # M1
    assert effective_rank(1, 2, 0) == 2      # M2 (protected by M1)
    assert effective_rank(3, 1, 2) == 3      # M3 (upgrade)
    assert effective_rank(1, 3, 1) == 3      # M4 (protected by M3)
    assert effective_rank(1, 1, 3) == 3      # M5 (last protected)
    assert effective_rank(1, 1, 1) == 1      # M6 (protection expired)


def test_effective_rank_business_example_2():
    # achieved months: M1=3, M2=1, M3=1, M4=1
    assert effective_rank(3, 0, 0) == 3      # M1
    assert effective_rank(1, 3, 0) == 3      # M2
    assert effective_rank(1, 1, 3) == 3      # M3
    assert effective_rank(1, 1, 1) == 1      # M4 (expired -> R1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_payout_ranks.py::test_effective_rank_is_max_of_window -v`
Expected: FAIL with `ImportError: cannot import name 'effective_rank'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to bot/payout/ranks.py
def effective_rank(achieved_current: int, achieved_prev1: int, achieved_prev2: int) -> int:
    """Effective rank = highest rank reached this month or in the previous two.

    A rank earned in month M protects M+1 and M+2 as a floor; upgrades apply
    immediately. Missing history is passed as 0. See PAYOUT_BUSINESS_LOGIC.md §4.3.
    """
    return max(achieved_current, achieved_prev1, achieved_prev2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_payout_ranks.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/payout/ranks.py tests/test_payout_ranks.py
git commit -m "feat(payout): effective-rank carry-over (max over 3-month window)"
```

---

### Task 3: Per-tranche commission and net

**Files:**
- Create: `bot/payout/commission.py`
- Test: `tests/test_payout_commission.py`

**Interfaces:**
- Consumes: `bot.payout.ranks.fee_pct`, `is_payable`.
- Produces:
  - `commission_for(amount: Decimal, rank: int) -> Decimal` — `amount * fee_pct(rank)`; rank 1–4.
  - `net_for(amount: Decimal, rank: int) -> Decimal` — `amount - commission_for(...)`; rank 1–4.
  - `Tranche` = `tuple[Decimal, int]` (amount, rank).
  - `totals(tranches: list[Tranche]) -> tuple[Decimal, Decimal, Decimal]` — `(gross, commission, net)` summed; skips non-payable (rank 0) tranches entirely.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_payout_commission.py
from decimal import Decimal

import pytest

from bot.payout.commission import commission_for, net_for, totals


def test_commission_and_net_per_rank():
    assert commission_for(Decimal("100"), 1) == Decimal("10.00")
    assert net_for(Decimal("100"), 1) == Decimal("90.00")
    assert commission_for(Decimal("200"), 2) == Decimal("14.00")
    assert net_for(Decimal("200"), 2) == Decimal("186.00")
    assert commission_for(Decimal("50"), 4) == Decimal("0")


def test_commission_rejects_rank_zero():
    with pytest.raises(ValueError):
        commission_for(Decimal("5"), 0)


def test_totals_owner_example():
    # $100 @ R1 (10%) + $200 @ R2 (7%) => commission $24, net $276.
    gross, commission, net = totals([(Decimal("100"), 1), (Decimal("200"), 2)])
    assert gross == Decimal("300")
    assert commission == Decimal("24.00")
    assert net == Decimal("276.00")


def test_totals_skips_rank_zero_tranches():
    gross, commission, net = totals([(Decimal("5"), 0), (Decimal("100"), 1)])
    assert gross == Decimal("100")       # rank-0 tranche excluded (non-payable)
    assert commission == Decimal("10.00")
    assert net == Decimal("90.00")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_payout_commission.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.payout.commission'`

- [ ] **Step 3: Write minimal implementation**

```python
# bot/payout/commission.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_payout_commission.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/payout/commission.py tests/test_payout_commission.py
git commit -m "feat(payout): per-tranche commission and net totals"
```

---

### Task 4: Accrual availability and expiry dates

**Files:**
- Create: `bot/payout/expiry.py`
- Test: `tests/test_payout_expiry.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `PAYOUT_TZ: timezone` — UTC+8.
  - `LIFETIME_DAYS: int = 30`.
  - `available_at(accrual_date: date) -> datetime` — start of D+1 in PAYOUT_TZ, returned as UTC-aware.
  - `expires_at(accrual_date: date, grace_days: int = 0) -> datetime` — start of D+30+grace in PAYOUT_TZ, UTC-aware.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_payout_expiry.py
from datetime import date, datetime, timezone

from bot.payout.expiry import LIFETIME_DAYS, available_at, expires_at


def test_available_at_is_start_of_next_day_utc8():
    # 2026-07-05 rebate becomes available at 2026-07-06 00:00 UTC+8 = 2026-07-05 16:00 UTC.
    assert available_at(date(2026, 7, 5)) == datetime(2026, 7, 5, 16, 0, tzinfo=timezone.utc)


def test_expires_at_is_start_of_day_d_plus_30_utc8():
    # 2026-07-05 + 30 days = 2026-08-04 00:00 UTC+8 = 2026-08-03 16:00 UTC.
    assert expires_at(date(2026, 7, 5)) == datetime(2026, 8, 3, 16, 0, tzinfo=timezone.utc)


def test_expires_at_applies_grace():
    # +2 grace days => 2026-08-06 00:00 UTC+8 = 2026-08-05 16:00 UTC.
    assert expires_at(date(2026, 7, 5), grace_days=2) == datetime(
        2026, 8, 5, 16, 0, tzinfo=timezone.utc
    )


def test_lifetime_is_30():
    assert LIFETIME_DAYS == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_payout_expiry.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.payout.expiry'`

- [ ] **Step 3: Write minimal implementation**

```python
# bot/payout/expiry.py
"""Accrual availability, expiry and withdrawal-cooldown date math.

Day boundaries are in PAYOUT_TIMEZONE (UTC+8). A day-D accrual is available from
the start of D+1 and expires at the start of D+30 (+grace). All returned datetimes
are UTC-aware for storage. See PAYOUT_BUSINESS_LOGIC.md §3.1, §5.1.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

PAYOUT_TZ = timezone(timedelta(hours=8))  # UTC+8
LIFETIME_DAYS = 30


def _day_start_utc(d: date) -> datetime:
    """Start of calendar day ``d`` in PAYOUT_TZ, as a UTC-aware datetime."""
    return datetime.combine(d, time.min, tzinfo=PAYOUT_TZ).astimezone(timezone.utc)


def available_at(accrual_date: date) -> datetime:
    """When a day-D accrual becomes withdrawable: start of D+1 (UTC+8), in UTC."""
    return _day_start_utc(accrual_date + timedelta(days=1))


def expires_at(accrual_date: date, grace_days: int = 0) -> datetime:
    """When a day-D accrual expires: start of D+30+grace (UTC+8), in UTC."""
    return _day_start_utc(accrual_date + timedelta(days=LIFETIME_DAYS + grace_days))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_payout_expiry.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/payout/expiry.py tests/test_payout_expiry.py
git commit -m "feat(payout): accrual availability and expiry date math (UTC+8)"
```

---

### Task 5: Withdrawal cooldown

**Files:**
- Modify: `bot/payout/expiry.py` (append `next_withdrawal_allowed`)
- Test: `tests/test_payout_expiry.py` (append)

**Interfaces:**
- Consumes: `bot.payout.ranks.cooldown_days`.
- Produces: `next_withdrawal_allowed(last_success_at: datetime | None, rank: int) -> datetime | None`
  — earliest next withdrawal time; `None` when there is no prior successful withdrawal
  (allowed immediately). Requires payable rank (1–4).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_payout_expiry.py
import pytest

from bot.payout.expiry import next_withdrawal_allowed


def test_cooldown_from_last_success():
    last = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert next_withdrawal_allowed(last, 3) == datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    assert next_withdrawal_allowed(last, 1) == datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def test_cooldown_none_when_no_history():
    assert next_withdrawal_allowed(None, 2) is None


def test_cooldown_rejects_rank_zero():
    with pytest.raises(ValueError):
        next_withdrawal_allowed(datetime(2026, 7, 1, tzinfo=timezone.utc), 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_payout_expiry.py::test_cooldown_from_last_success -v`
Expected: FAIL with `ImportError: cannot import name 'next_withdrawal_allowed'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to bot/payout/expiry.py (add import at top)
from bot.payout.ranks import cooldown_days


def next_withdrawal_allowed(last_success_at: datetime | None, rank: int) -> datetime | None:
    """Earliest next withdrawal = last successful withdrawal + cooldown(rank).

    Returns None when there is no prior successful withdrawal (allowed immediately
    once there is enough available balance — enforced elsewhere). Rank must be 1-4.
    """
    days = cooldown_days(rank)  # raises ValueError for rank 0
    if last_success_at is None:
        return None
    return last_success_at + timedelta(days=days)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_payout_expiry.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/payout/expiry.py tests/test_payout_expiry.py
git commit -m "feat(payout): withdrawal cooldown from last successful payout"
```

---

### Task 6: Package-wide quality gate

**Files:**
- No new files; runs the project gates over the new package.

- [ ] **Step 1: Run the full test suite**

Run: `uv run python -m pytest -q`
Expected: PASS (all existing + new payout tests)

- [ ] **Step 2: Lint the new package**

Run: `uv run ruff check bot/payout tests/test_payout_ranks.py tests/test_payout_commission.py tests/test_payout_expiry.py`
Expected: no new errors (Cyrillic-string RUF001 is the only repo-wide accepted pattern, and these files have none).

- [ ] **Step 3: Type-check**

Run: `uv run mypy bot/payout`
Expected: no errors in `bot/payout` (0 for the new package).

- [ ] **Step 4: Commit any fixes**

```bash
git add -A
git commit -m "chore(payout): pass ruff + mypy on payout domain core"
```

---

## Self-Review

**Spec coverage** (against PAYOUT_BUSINESS_LOGIC.md):
- §4.1 rank table / thresholds → Task 1. §4.3 carry-over → Task 2. §5.2 per-tranche
  commission → Task 3. §3.1 availability + 30-day expiry → Task 4. §5.1 cooldown → Task 5.
- Deliberately **out of scope** for this plan (separate plans): gas/net-after-gas (needs
  BNB→USDT, deferred per security §10.5), rank-0 burn orchestration, grace-from-downtime
  sourcing (`service_availability`), MIN_PAYOUT gating, FIFO selection over a real ledger,
  admin approval — all require DB/worker and are listed as follow-on plans below.

**Type consistency:** `rank: int` (0–4) and `Decimal` amounts used uniformly;
`Tranche = tuple[Decimal, int]`; datetimes UTC-aware throughout. `fee_pct`/`cooldown_days`
raise `ValueError` on rank 0 consistently.

**Placeholder scan:** none — every step has complete code and exact commands.

## Follow-on plans (not this document)

1. **DB schema & models** — migrations for the 11 tables in [DB_SCHEMA.md](../../../DB_SCHEMA.md).
2. **Accrual-worker** — daily accrual via existing exchange adapters, `rank_history`, grace journal.
3. **Bot UX** — wallet, UID, stats, withdraw button (FSM + action tokens).
4. **Payout-executor** — EVM send, nonce, idempotency, confirmations (testnet first).
5. **Admin/roles, kill-switch, alerts.**
