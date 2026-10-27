# Design: MNT rebate currency (Bybit) + admin personal-rate-by-nickname

Date: 2026-07-16
Status: approved (brainstorming) → pending implementation plan

Two independent features that both touch the commission/accrual/admin area, specced
together and implemented in one cycle.

---

## Feature A — MNT as a second rebate currency (Bybit only)

### Goal
Bybit invitees can earn affiliate commission in **MNT** as well as USDT. Today the
Bybit adapter records **only** USDT and silently drops MNT, so MNT rebate never reaches
the user. Add MNT as a fully-tracked second currency with the **same** rules as USDT
(daily accrual, 30-day expiry, rank tiers, cooldown, same rank-based fee — **no**
surcharge), valued in USDT-equivalent for rank/threshold/display, and paid out on-chain
by the bot **only after an admin manually funds the hot wallet and approves**.

### Decisions (from brainstorming)
- **Fee:** MNT uses the same `effective_fee(rank, override)` as USDT. No +5% (owner
  cancelled the surcharge).
- **Rank:** MNT counts toward the monthly rank tiers, valued in USDT-equivalent.
- **Lifecycle:** identical to USDT (daily accrual, `available_at`/`expires_at`, rank-0
  gate, cooldown).
- **Threshold:** withdrawable when net ≥ 10 **USDT-equivalent** (existing
  `MIN_PAYOUT_USDT`).
- **Price source:** Bybit public ticker `/v5/market/tickers?category=spot&symbol=MNTUSDT`
  (no auth). Rate captured and **frozen** at accrual time.
- **Chain (UPDATED 2026-07-17):** MNT is sent over the **Mantle network**, NOT BSC. On
  Mantle, MNT is the **native** coin, so this is a native-value transfer on a separate
  EVM chain — **no token contract**. Sent to the user's existing 0x address (EVM keys are
  chain-agnostic; same hot-wallet key/address on Mantle). Requires a Mantle RPC
  (`MANTLE_RPC_URL`), `MANTLE_CHAIN_ID` (default 5000). Gas on Mantle is paid in native
  MNT and is borne by the owner (no deduction). USDT stays a BEP20 token transfer on BSC,
  unchanged.
- **Payout path:** MNT payouts are **always** admin-gated (never auto), but once the
  admin approves ("I funded the wallet"), the **bot** sends MNT on-chain via the
  executor.
- **Pools:** USDT and MNT share **one** active-request limit and one cooldown (a user
  may have only one withdrawal in flight across both currencies).
- **Gas:** for MNT the owner bears BNB gas (no gas deduction from the MNT amount); only
  our commission is deducted. USDT is unchanged (gas deducted from net).

### Data model (migration)
`RebateAccrual` — the `*_usdt` columns are (re)interpreted as **USDT-equivalent** (already
true for USDT rows). Add:
- `amount_native NUMERIC(38,18)` — gross in the asset's own units.
- `net_native NUMERIC(38,18)` — payable amount in the asset's own units.
- `fx_rate NUMERIC(38,18)` — asset→USDT rate frozen at accrual (`1` for USDT).
- Backfill existing rows: `amount_native = amount_usdt`, `net_native = net_usdt`,
  `fx_rate = 1`.
- Change unique key `uq_accrual_day` from `(account_id, accrual_date)` →
  `(account_id, accrual_date, asset)` so a USDT **and** MNT row can coexist per day.

`Payout` — add:
- `asset VARCHAR NOT NULL DEFAULT 'USDT'`.
- `net_native NUMERIC(38,18)` — amount to transfer in the asset's units (for USDT ==
  `net_usdt`).

Because all rank/balance/threshold math already operates on `*_usdt`
(= USDT-equivalent), it keeps working unchanged and MNT automatically counts toward rank
and the 10-USDT threshold.

### Price feed
New helper (e.g. `bot/exchanges/price.py` or a method on the Bybit adapter):
`async def mnt_usdt_rate() -> Decimal` hitting the public ticker, `verify=True`,
timeout + one retry on 5xx/network. Fetched once per accrual cycle and passed into the
freeze. On failure the worker skips **MNT** accrual for that run (USDT unaffected) and
dev-alerts — never guesses a rate.

### Accrual worker
- Bybit adapter: stop dropping MNT — expose MNT as a `CommissionLine` alongside USDT
  (keep the "USDT only shown" note off; MNT is now first-class). Other coins still
  ignored.
- Worker: per account/day iterate the result's lines:
  - USDT line → USDT accrual (fee = rank fee/override), `native == usdt`, `fx_rate = 1`.
  - MNT line → value via `mnt_usdt_rate()`: `amount_usdt = mnt_native × rate`,
    `fee = rank fee/override` (same), `commission_usdt = amount_usdt × fee`,
    `net_usdt = amount_usdt − commission`, `net_native = mnt_native × (1 − fee)`,
    `fx_rate = rate`.
  - Both lines' USDT-equiv sum into the day's gross for rank.
  - Non-USDT/non-MNT lines ignored.
- `upsert_accrual` gains `asset`, `amount_native`, `net_native`, `fx_rate`; keyed on
  `(account_id, accrual_date, asset)`.

### Balance & withdraw (user)
- `balance_summary`: split available accruals by asset → a USDT pool and an MNT pool
  (MNT shown as native + USDT-equiv). Rank/next-withdrawal/expiry stay global.
- Cabinet: existing "💸 Вывести" becomes asset-aware. Show "Вывести USDT" and (only if
  MNT accruals exist) "Вывести MNT".
- `prepare_withdrawal(session, user_id, now, today, asset)` — filters available accruals
  to `asset`; eligibility identical (payable rank, net USDT-equiv ≥ 10, cooldown) **plus**
  the shared one-active-request rule: broaden the pending check from "reserved exists" to
  "any non-terminal payout exists" (`reserved`/`pending_admin`/`submitting`/`sent`) for
  the user, across both assets.
- MNT preview: shows MNT amount + USDT-equiv + note "выплата после подтверждения
  администратором".
- `confirm_withdrawal`: for MNT create `Payout(asset='MNT', net_native=…)` in
  `pending_admin` **always**; for USDT unchanged (auto if ≤ auto-limit else pending_admin).
  Confirm-time paused/blocked re-check (already added) applies to both.

### Payout routing & executor
- MNT payout → `pending_admin` on creation (regardless of amount).
- Admin queue (`💸 Крупные выплаты`) shows the asset. Release is asset-agnostic: MNT
  release means "wallet funded, send it" → `reserved`. Button label for MNT reads
  "✅ Пополнил кошелёк MNT — отправить".
- Executor `process_next` becomes **asset-aware**, routing by chain:
  - Claims `reserved` of any asset (unchanged claim), reads `payout.asset`.
  - For USDT: current behaviour on BSC (gas quote + deduction, USDT contract).
  - For MNT: use a **Mantle** chain client — **native** transfer of `net_native` MNT on
    Mantle (no contract), **no** gas deduction (owner bears native MNT gas), 18 decimals.
  - Nonce is **per-chain**: `last_nonce` must be scoped so BSC (USDT) and Mantle (MNT)
    nonce sequences don't mix; derive from `get_nonce(wallet)` on the relevant chain.
    Single-flight still processes one payout at a time.
  - Confirmations/paid path identical but polled against the payout's chain. A send
    failure (e.g. wallet not funded with MNT on Mantle) → `finalize_failed` + dev-alert;
    admin re-funds and re-approves.
- Second chain client instance pointed at `MANTLE_RPC_URL` with a native-transfer method;
  executor selects the client by `payout.asset`.

### Config
- `MANTLE_RPC_URL` — Mantle network RPC; required to enable MNT payouts.
- `MANTLE_CHAIN_ID` (default 5000, Mantle mainnet).
- MNT is native on Mantle → 18 decimals, no token contract.
- If `MANTLE_RPC_URL` is unset, MNT accrual/withdraw/payout is disabled (feature-flagged
  off) so the bot degrades safely.

### Admin stats
No change needed: earnings/profit already sum `commission_usdt` (USDT-equiv) across all
assets, so MNT commission flows into profit automatically. (A per-asset breakdown is
YAGNI for now.)

---

## Feature B — Admin personal commission rate by nickname (+ current-month recompute)

### Goal
Let an admin set a user's personal commission rate conveniently **by Telegram nickname**
(not only numeric id), and have it **also recompute the current month's not-yet-paid
accruals** — not just future ones.

### Decisions (from brainstorming)
- The value entered is **our commission %** (0–1), exactly as today's `🎚 Ставка юзеру`
  (`commission_override_pct`). Only the *entry by nickname* and the *retroactive
  current-month recompute* are new.
- Scope of recompute: **current month, not-yet-paid** accruals (state `available` or
  `accruing`), both assets. Reserved/paid/expired rows are left untouched (money already
  committed or gone). Frozen-fee principle is preserved for everything except the
  explicitly-requested current-month unpaid window.

### Username resolution
- Add `username VARCHAR NULL` to `User` (stored lowercased).
- Capture/refresh it on every interaction from a whitelisted user (in the access/DB
  middleware where `event_from_user` is available). Telegram gives no arbitrary
  `@username → id` lookup, so only users who have interacted with the bot are resolvable.
- Admin input accepts: `@nick <rate>`, `nick <rate>`, `<user_id> <rate>`, and
  `<nick|user_id> clear`. `@`/case are normalized. If a nickname is unknown →
  "Пользователь @nick не найден (должен был писать боту). Используйте числовой id."

### Set/clear flow
- `🎚 Ставка юзеру` prompt updated to mention nickname support.
- On **set**: `set_commission_override(user_id, pct, since=<current-month-start>)`
  (so future worker runs in this month also apply it), then **recompute** the user's
  current-month unpaid accruals:
  - For each row with `accrual_date` in current month, `state ∈ {available, accruing}`,
    `effective_rank ≥ 1`: `fee_pct = pct`, `commission_usdt = amount_usdt × pct`,
    `net_usdt = amount_usdt − commission_usdt`, and for native-bearing rows
    `net_native = amount_native × (1 − pct)`. Rank-0 rows (fee 0) are skipped.
  - Audit `commission_override_set` (+ a `recomputed_accruals` count).
- On **clear**: `clear_commission_override`, then recompute the same window back to the
  **rank default** fee per row: `fee = effective_fee(effective_rank, None)`; same
  commission/net/native recompute. Audit `commission_override_cleared`.
- Existing rowcount/validation issues noted in the audit (no-op on unknown user) are
  addressed by resolving via a real `User` row first; if neither nick nor id resolves to
  an existing user, report not-found instead of a silent success.

---

## Testing
- **A:** Bybit adapter emits MNT line; `mnt_usdt_rate` parse + error→skip-MNT; worker freezes
  two assets/day with correct native/usdt-equiv + fx_rate; MNT counts toward rank;
  threshold in USDT-equiv; withdraw MNT → `pending_admin`; admin release MNT → `reserved`;
  executor sends MNT via `MNT_CONTRACT` with no gas deduction (mock chain), USDT
  unchanged; shared one-active-request limit blocks a 2nd withdrawal across assets;
  `MNT_CONTRACT` unset → MNT disabled.
- **B:** username capture + case-insensitive resolve; unknown nick → error; set by
  nick recomputes current-month unpaid rows (both assets) and leaves
  reserved/paid/expired untouched; clear restores rank-default fee; rank-0 rows skipped;
  audit rows written.

## Migrations
1. `rebate_accruals`: add `amount_native`, `net_native`, `fx_rate`; backfill from
   `*_usdt`/1; swap `uq_accrual_day` to include `asset`.
2. `payouts`: add `asset` (default 'USDT'), `net_native`.
3. `users`: add `username`.

## Out of scope / assumptions
- MNT only on Bybit (only exchange that pays non-USDT here).
- MNT UI (button/pool) shown only when the user has MNT accruals.
- Per-asset admin-stats breakdown deferred (YAGNI).
- Owner bears MNT gas (no deduction).
