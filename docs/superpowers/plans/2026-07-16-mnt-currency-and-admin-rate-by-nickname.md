# MNT Currency + Admin Rate-by-Nickname Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add MNT as a second Bybit rebate currency (USDT-equiv rank/threshold, Bybit price, admin-gated but bot-executed BSC payout) and let admins set a personal commission rate by Telegram nickname with current-month unpaid-accrual recompute.

**Architecture:** Single-ledger, multi-asset. `RebateAccrual`/`Payout` gain native + fx columns while `*_usdt` stays the USDT-equivalent that all existing rank/balance/threshold math reads unchanged. MNT accrues daily like USDT (valued via Bybit MNTUSDT), always routes payouts through the admin queue, then the existing executor sends the MNT BEP20 token. Feature B extends the existing `commission_override` with nickname lookup + a bounded recompute of current-month unpaid rows.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0 async + asyncpg, Alembic, aiogram 3.x, httpx, pydantic v2, Decimal money, pytest-asyncio + testcontainers + pytest-httpx.

## Global Constraints

- Money is `Decimal` only — never `float`. Sum per-asset; never mix assets.
- Async everywhere for I/O; mypy-clean on touched files; `ruff` rules E/F/I/B/UP/ASYNC/S/C4/SIM/RUF (RUF001 Cyrillic is accepted baseline).
- Secrets from env only; never log secrets/addresses in full; `httpx verify=True`, timeouts + backoff (retry only network/5xx).
- Mark exchange-specific reasoning `# VERIFIED:` / `# ASSUMED:`.
- Tests must not hit live exchange APIs (httpx_mock fixtures); DB tests use the testcontainers Postgres `db_session` fixture.
- PAYOUT_TZ = UTC+8 governs "current month" / accrual-day bucketing everywhere.
- MNT is feature-flagged by `MANTLE_RPC_URL`: unset → MNT accrual/withdraw/payout disabled, bot behaves exactly as today.
- **CHAIN (updated 2026-07-17):** MNT is sent over the **Mantle** network as the **native** coin (native-value transfer, no token contract), to the user's same 0x address. USDT is unchanged (BEP20 token on BSC). Nonce sequences are per-chain.

---

## Task 1: Schema migration + model columns (accruals, payouts, users)

**Files:**
- Modify: `bot/db/models/ledger.py` (RebateAccrual)
- Modify: `bot/db/models/payouts.py` (Payout)
- Modify: `bot/db/models/identity.py` (User)
- Create: `alembic/versions/<rev>_mnt_multiasset_and_username.py`
- Test: `tests/db/test_multiasset_schema.py`

**Interfaces:**
- Produces: `RebateAccrual.amount_native`, `.net_native`, `.fx_rate` (Decimal); unique key `(account_id, accrual_date, asset)`. `Payout.asset` (str, default 'USDT'), `Payout.net_native` (Decimal). `User.username` (str|None, lowercased).

- [ ] **Step 1: Write the failing test** — `tests/db/test_multiasset_schema.py`

```python
from __future__ import annotations
from datetime import UTC, date, datetime
from decimal import Decimal
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_two_assets_same_account_day_coexist(db_session: AsyncSession):
    session = db_session
    session.add(User(id=800, role="user", status="active"))
    await session.flush()
    acc = ExchangeAccount(user_id=800, exchange="bybit", uid="U800",
                          status="active", accrual_since=date(2026, 7, 1))
    session.add(acc)
    await session.flush()

    def _row(asset: str, native: Decimal, rate: Decimal) -> RebateAccrual:
        usdt = native * rate
        return RebateAccrual(
            user_id=800, account_id=acc.id, accrual_date=date(2026, 7, 5), asset=asset,
            amount_usdt=usdt, amount_native=native, net_native=native, fx_rate=rate,
            effective_rank=1, fee_pct=Decimal("0"), commission_usdt=Decimal("0"),
            net_usdt=usdt, state="available",
            available_at=datetime(2026, 7, 6, tzinfo=UTC),
            expires_at=datetime(2026, 8, 4, tzinfo=UTC), raw_records_count=1,
        )

    session.add(_row("USDT", Decimal("50"), Decimal("1")))
    session.add(_row("MNT", Decimal("100"), Decimal("0.6")))
    await session.flush()
    rows = (await session.scalars(
        select(RebateAccrual).where(RebateAccrual.account_id == acc.id))).all()
    assert {r.asset for r in rows} == {"USDT", "MNT"}
    mnt = next(r for r in rows if r.asset == "MNT")
    assert mnt.amount_usdt == Decimal("60") and mnt.fx_rate == Decimal("0.6")
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/db/test_multiasset_schema.py -v` → FAIL (unknown columns / unique violation on `uq_accrual_day`).

- [ ] **Step 3: Add columns to models**

In `bot/db/models/ledger.py` `RebateAccrual`, after `net_usdt`:
```python
    amount_native: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, server_default="0")
    net_native: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, server_default="0")
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, server_default="1")
```
Change `__table_args__` unique: `UniqueConstraint("account_id", "accrual_date", "asset", name="uq_accrual_day")`.

In `bot/db/models/payouts.py` `Payout`, after `net_usdt`:
```python
    asset: Mapped[str] = mapped_column(String, nullable=False, server_default="USDT")
    net_native: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, server_default="0")
```

In `bot/db/models/identity.py` `User`, add:
```python
    username: Mapped[str | None] = mapped_column(String, nullable=True)
```

- [ ] **Step 4: Write the Alembic migration** — copy the newest file in `alembic/versions/` for style. Upgrade body:
```python
def upgrade() -> None:
    op.add_column("rebate_accruals", sa.Column("amount_native", sa.Numeric(38, 18), nullable=False, server_default="0"))
    op.add_column("rebate_accruals", sa.Column("net_native", sa.Numeric(38, 18), nullable=False, server_default="0"))
    op.add_column("rebate_accruals", sa.Column("fx_rate", sa.Numeric(38, 18), nullable=False, server_default="1"))
    op.execute("UPDATE rebate_accruals SET amount_native = amount_usdt, net_native = net_usdt WHERE amount_native = 0")
    op.drop_constraint("uq_accrual_day", "rebate_accruals", type_="unique")
    op.create_unique_constraint("uq_accrual_day", "rebate_accruals", ["account_id", "accrual_date", "asset"])
    op.add_column("payouts", sa.Column("asset", sa.String(), nullable=False, server_default="USDT"))
    op.add_column("payouts", sa.Column("net_native", sa.Numeric(38, 18), nullable=False, server_default="0"))
    op.execute("UPDATE payouts SET net_native = net_usdt WHERE net_native = 0")
    op.add_column("users", sa.Column("username", sa.String(), nullable=True))

def downgrade() -> None:
    op.drop_column("users", "username")
    op.drop_column("payouts", "net_native")
    op.drop_column("payouts", "asset")
    op.drop_constraint("uq_accrual_day", "rebate_accruals", type_="unique")
    op.create_unique_constraint("uq_accrual_day", "rebate_accruals", ["account_id", "accrual_date"])
    op.drop_column("rebate_accruals", "fx_rate")
    op.drop_column("rebate_accruals", "net_native")
    op.drop_column("rebate_accruals", "amount_native")
```
Set `down_revision` to the current head (`uv run alembic heads`).

- [ ] **Step 5: Run test to verify it passes** — `uv run pytest tests/db/test_multiasset_schema.py -v` → PASS. (The `db_session` fixture creates tables from models via metadata; migration is validated separately in CI/deploy.)

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(db): multi-asset accrual/payout columns + users.username"`

---

## Task 2: Config — Mantle RPC + feature flag

**Files:**
- Modify: `bot/config.py` (Settings, ~line 124 near `usdt_contract`)
- Test: `tests/test_config_mnt.py`

**Interfaces:**
- Produces: `Settings.mantle_rpc_url: str | None` (alias `MANTLE_RPC_URL`), `Settings.mantle_chain_id: int` (alias `MANTLE_CHAIN_ID`, default 5000), `Settings.mnt_enabled: bool` property (`mantle_rpc_url is not None`). MNT is native on Mantle → 18 decimals, no token contract.

- [ ] **Step 1: Failing test** — `tests/test_config_mnt.py`
```python
from bot.config import Settings
from tests.conftest import make_settings  # existing helper


def test_mnt_disabled_by_default():
    s = make_settings()
    assert s.mnt_enabled is False


def test_mnt_enabled_when_mantle_rpc_set():
    s = make_settings(mantle_rpc_url="https://rpc.mantle.xyz", mantle_chain_id=5000)
    assert s.mnt_enabled is True and s.mantle_chain_id == 5000
```
(If `make_settings` doesn't accept these kwargs, extend it to pass through overrides — check `tests/conftest.py` first and adapt.)

- [ ] **Step 2: Run → FAIL** (`mnt_enabled` missing).

- [ ] **Step 3: Implement** in `bot/config.py`:
```python
    mantle_rpc_url: str | None = Field(default=None, alias="MANTLE_RPC_URL")
    mantle_chain_id: int = Field(default=5000, alias="MANTLE_CHAIN_ID")

    @property
    def mnt_enabled(self) -> bool:
        return self.mantle_rpc_url is not None
```

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(config): MANTLE_RPC_URL feature flag for MNT payouts"`

---

## Task 3: Bybit MNTUSDT price feed

**Files:**
- Modify: `bot/exchanges/bybit.py` (add public-ticker method) OR Create `bot/exchanges/price.py`
- Test: `tests/exchanges/test_bybit_price.py`

**Interfaces:**
- Produces: `BybitAdapter.mnt_usdt_rate() -> Decimal` — GET `/v5/market/tickers?category=spot&symbol=MNTUSDT`, returns `Decimal(result.list[0].lastPrice)`. Raises `ExchangeApiError` on missing/non-parseable price (caller treats as skip-MNT).

- [ ] **Step 1: Failing test** with `httpx_mock`:
```python
import pytest
from decimal import Decimal
from bot.exchanges.registry import build_adapter
from tests.conftest import make_settings
pytestmark = pytest.mark.asyncio(loop_scope="session")

async def test_mnt_usdt_rate_parses_last_price(httpx_mock):
    httpx_mock.add_response(
        url="https://api.bybit.com/v5/market/tickers?category=spot&symbol=MNTUSDT",
        json={"retCode": 0, "result": {"list": [{"symbol": "MNTUSDT", "lastPrice": "0.5321"}]}})
    ad = build_adapter(make_settings(bybit=True), "bybit")
    assert await ad.mnt_usdt_rate() == Decimal("0.5321")
    await ad.aclose()
```

- [ ] **Step 2: Run → FAIL** (`mnt_usdt_rate` missing).

- [ ] **Step 3: Implement** in `bot/exchanges/bybit.py`:
```python
_TICKERS_PATH = "/v5/market/tickers"

async def mnt_usdt_rate(self) -> Decimal:
    """Public spot MNTUSDT last price (no auth). VERIFIED: Bybit v5 market/tickers."""
    from urllib.parse import urlencode
    q = urlencode({"category": "spot", "symbol": "MNTUSDT"})
    payload = await self._send("GET", f"{_TICKERS_PATH}?{q}", headers={"Accept": "application/json"}, log_path=_TICKERS_PATH)
    result = self._extract_result(payload)
    rows = result.get("list")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        price = rows[0].get("lastPrice")
        if price not in (None, ""):
            return parse_decimal(price)
    raise ExchangeApiError(f"{self.name}: no MNTUSDT price")
```

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(bybit): public MNTUSDT price feed"`

---

## Task 4: Bybit adapter emits MNT commission line

**Files:**
- Modify: `bot/exchanges/bybit.py` (`_add_usdt_commission` → record MNT too)
- Test: `tests/test_adapters.py` (add a Bybit MNT case) or `tests/exchanges/test_bybit_mnt.py`

**Interfaces:**
- Produces: for a `commissionsVol` with USDT and MNT, `get_commission(...).lines` contains both an `asset="USDT"` and `asset="MNT"` line (other coins still ignored).

- [ ] **Step 1: Failing test** — build the adapter, mock `aff-user-list` returning `commissionsVol={"USDT":"0.5","MNT":"100","BTC":"0"}` for the uid; assert both USDT and MNT lines present, BTC absent.
- [ ] **Step 2: Run → FAIL** (only USDT line today).
- [ ] **Step 3: Implement** — in `_add_usdt_commission` (rename intent, keep name to limit churn or rename to `_add_commissions`): additionally record MNT:
```python
    MNT = "MNT"
    if isinstance(value, dict):
        for coin, amount in value.items():
            c = str(coin).upper()
            if c in ("USDT", "MNT"):
                amt = parse_decimal(amount or 0)
                if amt != 0:
                    result.add_amount(c, amt)
            elif self._nonzero(amount):
                other_coins_present = True
```
Drop the "USDT only" note; keep the "other coins ignored" note for non-USDT/MNT.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(bybit): expose MNT commission as a line"`

---

## Task 5: Accrual worker — two-asset freeze + MNT valuation + rank inclusion

**Files:**
- Modify: `bot/accrual/worker.py` (per-account line loop, freeze both assets)
- Modify: `bot/accrual/repository.py` (`upsert_accrual` gains asset/native/fx)
- Test: `tests/accrual/test_worker_mnt.py`

**Interfaces:**
- Consumes: `BybitAdapter.mnt_usdt_rate()`, `Settings.mnt_enabled`, `CommissionResult.lines`.
- Produces: worker freezes one `RebateAccrual` per (account, day, asset). Day gross for rank = Σ `amount_usdt` across both assets. `upsert_accrual(..., asset, amount_native, net_native, fx_rate)`.

- [ ] **Step 1: Failing test** — seed a Bybit account; stub its adapter to return a result whose `.lines` = `[USDT 50, MNT 100]` and stub `mnt_usdt_rate → 0.6`; run one day; assert two accrual rows (USDT amount_usdt 50 native 50; MNT amount_usdt 60 native 100 fx 0.6), both with the same rank driven by day gross 110, MNT `net_native = 100×(1−fee)`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — replace `amount = usdt_of(result)` single-value logic with a per-asset list. Sketch:
```python
# gather (asset, native, usdt_equiv) per account
rate = None
async def _rate() -> Decimal:
    nonlocal rate
    if rate is None:
        rate = await bybit_adapter.mnt_usdt_rate()
    return rate

per_account_assets: list[tuple[int, str, Decimal, Decimal, Decimal, int, str | None]] = []
for acc in accounts:
    ...
    result = await adapter.get_commission(acc.uid, day_start, day_end)
    for line in result.lines:
        if line.asset == "USDT":
            per_account_assets.append((acc.id, "USDT", line.amount, line.amount, Decimal(1), result.raw_records_count, note))
            day_amount += line.amount
        elif line.asset == "MNT" and settings.mnt_enabled:
            r = await _rate()
            usdt_equiv = line.amount * r
            per_account_assets.append((acc.id, "MNT", line.amount, usdt_equiv, r, result.raw_records_count, note))
            day_amount += usdt_equiv
```
Then in the freeze loop compute `fee` per row (rank-based, same for both assets), `commission_usdt = usdt_equiv * fee`, `net_usdt = usdt_equiv - commission_usdt`, `net_native = native * (1 - fee)`; call `upsert_accrual(..., asset=asset, amount_usdt=usdt_equiv, amount_native=native, net_native=net_native, fx_rate=fx)`.
Guard: if `mnt_usdt_rate()` raises, log + `_dev_alert`, skip MNT rows for this run (USDT still frozen).
`upsert_accrual` in `bot/accrual/repository.py`: add params `asset="USDT", amount_native, net_native, fx_rate`; include in insert and in the conflict target `(account_id, accrual_date, asset)`.

- [ ] **Step 4: Run → PASS** + run existing `tests/accrual` to confirm USDT path intact.
- [ ] **Step 5: Commit** — `git commit -am "feat(accrual): freeze MNT alongside USDT, rank counts USDT-equiv"`

---

## Task 6: Balance summary split by asset

**Files:**
- Modify: `bot/botui/repository.py` (`available_accruals` optional asset filter; helper sums)
- Modify: `bot/botui/service.py` (`balance_summary` → per-asset pools)
- Test: `tests/botui/test_stats.py` (extend)

**Interfaces:**
- Produces: `BalanceSummary` gains `mnt_native: Decimal`, `mnt_usdt: Decimal`, `mnt_withdrawable: bool` (USDT fields unchanged). `available_accruals(session, user_id, asset=None)`.

- [ ] **Step 1: Failing test** — user with USDT + MNT available rows; `balance_summary` returns USDT net and separately `mnt_native`/`mnt_usdt`; rank driven by combined USDT-equiv.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `available_accruals` gains `asset: str | None = None` (filter when set). In `balance_summary`, split: USDT pool from `asset='USDT'`, MNT pool from `asset='MNT'`; `mnt_usdt = Σ net_usdt(MNT)`, `mnt_native = Σ net_native(MNT)`; `mnt_withdrawable = is_payable(rank) and mnt_usdt >= MIN_PAYOUT_USDT and cooldown_ok and not any_active_payout`. Keep `available_net` = USDT-only pool to preserve current USDT semantics.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(botui): per-asset balance pools (USDT + MNT)"`

---

## Task 7: Withdraw eligibility + confirm, asset-aware + shared single-request

**Files:**
- Modify: `bot/botui/repository.py` (`has_active_payout` replacing/augmenting `has_active_reserved_payout`)
- Modify: `bot/botui/service.py` (`prepare_withdrawal(..., asset)`, `confirm_withdrawal` → asset, MNT→pending_admin)
- Modify: `bot/botui/repository.py` (`reserve_accruals_and_create_payout` gains asset + net_native + initial status)
- Test: `tests/botui/test_withdraw.py`, `tests/botui/test_withdraw_blocked.py` (extend)

**Interfaces:**
- Consumes: per-asset pools from Task 6.
- Produces: `prepare_withdrawal(session, user_id, now, today, asset="USDT")`; `has_active_payout(session, user_id)` (any of reserved/pending_admin/submitting/sent); `reserve_accruals_and_create_payout(..., asset, net_native, initial_status)`.

- [ ] **Step 1: Failing tests** —
  (a) `has_active_payout` true for a `pending_admin` payout (not just reserved).
  (b) A user with a pending USDT payout is blocked (`pending`) from starting an MNT withdrawal.
  (c) `confirm_withdrawal(..., asset="MNT")` creates a `Payout(asset='MNT', status='pending_admin', net_native=…)` even below the auto-limit.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement:**
  - Add `has_active_payout`: `select(exists().where(user_id==, status.in_(('reserved','pending_admin','submitting','sent'))))`. Replace the `has_active_reserved_payout` call in `prepare_withdrawal` with `has_active_payout`; keep the old fn or delete if unused.
  - `prepare_withdrawal(..., asset="USDT")`: pull `available_accruals(..., asset)`, sum that asset's pool, same eligibility; `WithdrawPreview` gains `asset` + `net_native`.
  - `issue_withdraw_token` payload includes `asset` + `net_native`.
  - `confirm_withdrawal`: after the (existing) paused/blocked re-check + token consume, pass `asset` + `net_native` to `reserve_accruals_and_create_payout`, with `initial_status = 'pending_admin' if asset=='MNT' else ('pending_admin' if net>auto_limit else 'reserved')`. NOTE: the USDT auto-limit routing currently lives in the executor (`process_next` → `mark_pending_admin`), so keep USDT initial status `reserved` and only force MNT to `pending_admin` here; the executor still routes large USDT. So: `initial_status='pending_admin' if asset=='MNT' else 'reserved'`.
  - `reserve_accruals_and_create_payout(..., asset='USDT', net_native, initial_status='reserved')`: set `Payout.asset`, `.net_native`, `.status=initial_status`.
- [ ] **Step 4: Run → PASS** + full `tests/botui`.
- [ ] **Step 5: Commit** — `git commit -am "feat(withdraw): asset-aware preview/confirm; MNT→admin queue; shared single active request"`

---

## Task 8: Withdraw UI — asset buttons

**Files:**
- Modify: `bot/botui/keyboards.py` (withdraw entry: USDT + MNT buttons; `WithdrawStartCB(asset)`)
- Modify: `bot/botui/withdraw.py` (start handler reads asset; preview text for MNT)
- Modify: `bot/botui/menu.py`/cabinet keyboard if the withdraw button lives there
- Test: `tests/botui/test_withdraw.py` (handler)

**Interfaces:**
- Consumes: `prepare_withdrawal(..., asset)`.
- Produces: `WithdrawStartCB(CallbackData, prefix="wd")` with `asset: str`; MNT button shown only when `mnt_native > 0`.

- [ ] **Step 1: Failing test** — tapping the MNT withdraw callback calls `prepare_withdrawal(asset="MNT")` and the preview text contains "MNT" + "после подтверждения администратором".
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — add `WithdrawStartCB`; cabinet/withdraw entry renders "💸 Вывести USDT" always and "💸 Вывести MNT" when the MNT pool > 0 (query balance first). `start_withdraw` becomes `WithdrawStartCB.filter()` handler reading `callback_data.asset`. MNT preview appends the admin-confirmation note + shows `net_native` MNT and USDT-equiv.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(botui): USDT/MNT withdraw buttons + MNT preview note"`

---

## Task 9: Executor — asset-aware send (MNT native on Mantle)

**Files:**
- Modify: `bot/executor/chain.py` (add native-transfer support; a Mantle client)
- Modify: `bot/executor/executor.py` (`process_next` routes by `payout.asset`/chain; per-chain nonce; MNT no gas deduction)
- Modify: `bot/executor/repository.py` (`last_nonce` scoped per asset/chain)
- Test: `tests/executor/test_process_next.py` (MNT case), `tests/executor/test_chain.py`

**Interfaces:**
- Consumes: `Settings.mantle_rpc_url`, `Settings.mantle_chain_id`, `Payout.asset`, `Payout.net_native`.
- Produces: executor sends `net_native` on the asset's chain — USDT via BEP20 token transfer on BSC (unchanged), MNT via **native** transfer on Mantle. Per-chain nonce.

**IMPORTANT — inspect `bot/executor/chain.py` first.** It currently implements USDT-on-BSC as an ERC20/BEP20 **token contract** transfer (`send_transfer`, `usdt_to_wei`, nonce/confirmation helpers via a Web3 client). MNT on Mantle is a **native-coin** transfer on a **different EVM chain** — a separate RPC, separate chain-id, separate nonce space, and gas paid in native MNT (owner bears, no deduction). Do NOT reuse the BSC client for MNT.

- [ ] **Step 1: Failing test** — with a Mantle client injected (mock), a `reserved` MNT payout (`net_native=100`) is processed: the mock **Mantle** client records a **native** transfer of the full 100 (no gas deduction) to the recipient, payout → `sent`; a USDT payout still uses the BSC token client with gas deduction. Assert the MNT send used the Mantle client, not the BSC one.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement:**
  - In `chain.py`: add a native-transfer method (e.g. `send_native(to, amount_wei, nonce, on_signed)`) and a Mantle client built from `mantle_rpc_url`/`mantle_chain_id` (reuse the existing Web3 client class with a `native=True`/no-contract mode, or a small subclass — follow the existing class's structure). Add decimals-aware `to_wei(amount, 18)` for native MNT if not already generic.
  - `process_next` selects the client + mode by `payout.asset`:
    - USDT → BSC token client, gas quote + deduction, unchanged.
    - MNT → Mantle client, **native** transfer of `payout.net_native`, **no** gas quote/deduction (`gas_usdt=0`), nonce from the Mantle chain.
    - Only `reserved` reaches `process_next`; MNT is never auto (enters as `pending_admin`), so the USDT auto-limit branch is untouched.
  - `repository.last_nonce` must be scoped per chain/asset (e.g. `last_nonce(session, asset)` → `max(Payout.nonce) WHERE asset=...`) so BSC and Mantle nonce sequences never mix. Update both call site and the USDT path to pass its asset.
  - Inject the Mantle client (or `None` when `mnt_enabled` is False) + settings into `PayoutExecutor.__init__`; wire in `main.py` (Task 13). When the Mantle client is `None` and an MNT payout is claimed → `finalize_failed(pid, "mnt_disabled")` + dev-alert (defensive; MNT withdraw is gated off when disabled anyway).
  - Confirmations (`poll_confirmations`, `reconcile`) must poll the correct chain per payout asset.
- [ ] **Step 4: Run → PASS** + full `tests/executor`.
- [ ] **Step 5: Commit** — `git commit -am "feat(executor): MNT native transfer on Mantle, per-chain nonce"`

---

## Task 10: Admin queue — asset display + MNT approval label

**Files:**
- Modify: `bot/admin/router.py` (`_payout_summary` shows asset; MNT button label)
- Modify: `bot/admin/keyboards.py` (`payout_action_keyboard` label varies by asset — pass asset in)
- Test: `tests/admin/test_release.py` (extend: MNT payout release → reserved)

**Interfaces:**
- Consumes: `Payout.asset`, `.net_native`.
- Produces: admin summary line includes asset + native amount; MNT "release" button reads "✅ Пополнил кошелёк MNT — отправить"; `release_payout` unchanged (asset-agnostic → `reserved`).

- [ ] **Step 1: Failing test** — a `pending_admin` MNT payout listed shows "MNT" + native amount; releasing it flips to `reserved` (executor then handles it).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `_payout_summary` adds `Актив: {payout.asset}` and for MNT shows `{net_native} MNT (~{net_usdt} USDT)`. `payout_action_keyboard(payout_id, asset)` sets the release button text per asset. No repo change (release already just flips to reserved).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(admin): show asset in payout queue; MNT approve label"`

---

## Task 11: Username capture + resolve

**Files:**
- Modify: `bot/security/access.py` or `bot/botui/middleware.py` (capture `username` on interaction)
- Modify: `bot/botui/repository.py` (`upsert_user` stores username; `find_user_id_by_username`)
- Test: `tests/botui/test_username_resolve.py`

**Interfaces:**
- Produces: `upsert_user(session, user_id, username=None)` persists lowercased username; `find_user_id_by_username(session, username) -> int | None` (case-insensitive, strips leading `@`).

- [ ] **Step 1: Failing test** — `upsert_user(s, 42, username="Whale")` then `find_user_id_by_username(s, "@whale") == 42`; unknown → None.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `upsert_user` gains `username: str | None = None`; on create/existing set `user.username = username.lower()` when provided. `find_user_id_by_username`: `select(User.id).where(func.lower(User.username) == username.strip().lstrip("@").lower())`. Capture point: in the DB-session middleware where the user row is ensured, pass `event.from_user.username`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(identity): capture + resolve Telegram username"`

---

## Task 12: Admin personal rate by nickname + current-month recompute

**Files:**
- Modify: `bot/admin/service.py` (`parse_rate_command` accepts nick or id)
- Modify: `bot/admin/router.py` (`receive_rate` resolves nick → id; calls recompute)
- Modify: `bot/admin/repository.py` (`recompute_current_month_unpaid(session, user_id, pct_or_none, month_start, month_before)`)
- Test: `tests/admin/test_controls.py` (extend), `tests/admin/test_rate_recompute.py`

**Interfaces:**
- Consumes: `find_user_id_by_username`, `bot.payout.commission.effective_fee`, `bot.payout.ranks`.
- Produces: `parse_rate_command(text) -> tuple[str, Decimal | None] | None` where the first element is a raw target token (nick or id); `recompute_current_month_unpaid(...)` updates fee/commission/net(/native) for `state ∈ {available,accruing}`, current month, `effective_rank ≥ 1`.

- [ ] **Step 1: Failing tests** —
  (a) `receive_rate` with `@nick 0.02` resolves the nick to the user and sets override.
  (b) After setting `0.02` for a user with a current-month `available` USDT row (amount_usdt 100, was fee 0.10 net 90), the row becomes fee 0.02 / commission 2 / net 98; a `reserved` row is untouched; a `paid` row is untouched; an MNT row updates `net_native` too.
  (c) `clear` restores the rank-default fee (`fee_pct(effective_rank)`).
  (d) Unknown nick → error message, no change.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement:**
  - `parse_rate_command`: split into `<target> <rate|clear>`; return `(target_str, pct_or_None)`; validate rate ∈ [0,1] when numeric.
  - `receive_rate`: if `target_str` is all digits → `user_id = int(target)`; else `user_id = await repo.find_user_id_by_username(session, target)`; if None → reply "Пользователь {target} не найден (должен был писать боту). Используйте числовой id." and return. Then set/clear override with `since = month_start(today_payout_tz)`, then `recompute_current_month_unpaid(...)`, then audit (+ recomputed count), then confirm message.
  - `recompute_current_month_unpaid(session, user_id, pct, m_start, m_before)`:
```python
rows = (await session.scalars(select(RebateAccrual).where(
    RebateAccrual.user_id == user_id,
    RebateAccrual.accrual_date >= m_start,
    RebateAccrual.accrual_date < m_before,
    RebateAccrual.state.in_(("available", "accruing")),
    RebateAccrual.effective_rank >= 1,
))).all()
n = 0
for r in rows:
    fee = pct if pct is not None else fee_pct(r.effective_rank)
    r.fee_pct = fee
    r.commission_usdt = r.amount_usdt * fee
    r.net_usdt = r.amount_usdt - r.commission_usdt
    r.net_native = r.amount_native * (Decimal(1) - fee)
    n += 1
await session.flush()
return n
```
  - Update `_RATE_PROMPT` to mention nickname support.
- [ ] **Step 4: Run → PASS** + full `tests/admin`.
- [ ] **Step 5: Commit** — `git commit -am "feat(admin): personal rate by nickname + current-month recompute"`

---

## Task 13: Wiring, env docs, full suite + deploy prep

**Files:**
- Modify: `bot/main.py` (inject settings/mnt config into `PayoutExecutor`; pass username to `upsert_user`)
- Modify: `.env.example` / `README.md` (document `MNT_CONTRACT`, `MNT_DECIMALS`)
- Test: full `uv run pytest -q`; `uv run mypy bot`

- [ ] **Step 1:** Wire `PayoutExecutor(...)` in `bot/main.py`: build the Mantle chain client from `settings.mantle_rpc_url`/`mantle_chain_id` when `settings.mnt_enabled`, else pass `None`; inject it + settings alongside the existing BSC client.
- [ ] **Step 2:** Document new env in `.env.example` and README (MNT section: `MANTLE_RPC_URL`, `MANTLE_CHAIN_ID`=5000, MNT is native on Mantle so no contract; unset `MANTLE_RPC_URL` disables MNT).
- [ ] **Step 3:** `uv run pytest -q` → all green; `uv run mypy bot` → no new errors (baseline 23 in `exchanges/` unchanged).
- [ ] **Step 4:** `uv run ruff format` touched files; `uv run ruff check bot` (no new non-RUF001 classes).
- [ ] **Step 5: Commit** — `git commit -am "chore(mnt): wire executor config + document env"`

---

## Self-review notes
- Spec coverage: Feature A → Tasks 1–10, 13; Feature B → Tasks 1 (username col), 11–12. Price feed → 3. Rank inclusion → 5. Threshold USDT-equiv → 6/7 (reuses `MIN_PAYOUT_USDT` on `net_usdt`). Shared single-request → 7. Gas-none for MNT → 9. Admin stats → already covered (no task; profit sums commission_usdt across assets).
- Type consistency: `net_native`/`amount_native`/`fx_rate` (Decimal) consistent Tasks 1/5/6/7/9/12; `asset` str consistent 1/7/9/10; `find_user_id_by_username`/`recompute_current_month_unpaid` names consistent 11/12.
- Feature flag: `mnt_enabled` gates worker MNT freeze (Task 5) and MNT withdraw button (Task 8); with it off the bot equals today.
