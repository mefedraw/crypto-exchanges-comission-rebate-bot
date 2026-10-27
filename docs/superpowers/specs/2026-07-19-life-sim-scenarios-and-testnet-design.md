# Life-sim (100 days / 50 users) with scenarios + real testnet — Design

Date: 2026-07-19
Status: Approved (pending spec review)

## Goal

Simulate ~3.3 months of the bot's life (100 days, 50 users) on an isolated DB,
exercising a broad, deliberately-designed set of scenarios and asserting money/state
invariants — then prove the executor's on-chain send paths on **real testnets** with a
bounded set of payouts, using the funded server wallet's key without extracting it.

## Constraints (grounding facts)

- **Testnet USDT is tiny: ~3.67 USDT** on the wallet (BSC testnet token
  `0x337610d27c682E347C9cD60BD4b3b107C9d34dDd`, 18 decimals). tBNB ~0.77 (gas is fine).
  Mantle Sepolia MNT ~10.
- Therefore each real USDT send is a **fixed 0.01 USDT** (not sim-amount/1000 — whale
  amounts/1000 are still $1–5 and would drain 3.67 fast). Fixed 0.01 → 3.67 covers ~360
  sends; **self-send (to == from) costs no USDT at all**, only gas.
- The funded testnet wallet is the **production hot wallet** (`0x2783…Ef72`); its key is
  the mainnet `PAYOUT_PRIVATE_KEY` in the server `.env`. Real sends therefore run
  **on the server**, key read from the container env, never extracted or printed.
- Server is RAM-tight (1 GB, already swapping). The real-testnet pass must NOT spin up a
  heavy DB: it uses a **throwaway `simtest` database inside the already-running postgres**
  (the `bot` role is superuser there), then `DROP DATABASE simtest` after. Prod `bot` DB
  is never touched.

## Realistic earning profiles (grounded to ≤ ~$10k rebates)

Rank thresholds are fixed in code (monthly gross USDT): r1 $10–100, r2 $100–1000,
r3 $1000–10000, r4 >$10000. "Rebates up to ~$10k" ⇒ realistic top is **rank 3**; rank 4 is
an occasional whale spike, not the norm.

| Rank | Users | ~$/day per account | Cumulative rebate over sim |
|------|-------|--------------------|----------------------------|
| 0 (dust) | 6 | 0.05–0.25 | ~$5–25 |
| 1 | 15 | 0.4–2.5 | ~$40–250 |
| 2 | 15 | 4–25 | ~$400–2500 |
| 3 | 11 | 30–90 | ~$3000–9000 |
| 4 (spike) | 3 | one peak month ~$11–13k (daily ~$400 that month), else r2–3 | ~$10–18k |

- Total across 50 users ≈ **$80–150k** over ~3.3 months — plausible for a 50-referral
  program. NO $1M+ figures.
- The rank-4 spike (3 users, one elevated month each) exists solely to exercise rank-4
  logic; drop it and cap at r3 only if a hard $10k ceiling is later required.
- ~⅓ of non-dust users also hold a **Bybit + MNT** account (dual-asset accrual).

## Architecture — two parts

### Part A — full life-sim (local, mock chain)

Extends the existing `sim/harness.py` + `sim/fakes.py`. Runs 100 days × 50 users against an
isolated testcontainers Postgres with `MockChainClient` (two instances: BSC + Mantle, per
-chain nonce). Drives the REAL accrual worker, botui withdrawal service, admin repository,
and payout executor via a day loop with a controllable clock, injecting scenario events on
scheduled days, then prints a report and asserts invariants. Fast (minutes), no funds.

### Part B — bounded real-testnet executor pass (server)

A new server-side script (run in a one-off container from `commission-rebate-bot:latest`,
like the earlier `testnet_send.py`) that:
1. Creates throwaway DB `simtest` in the running postgres; `Base.metadata.create_all`.
2. Seeds ~30 payouts (+ their users/wallets/reserved accruals) spanning the on-chain
   scenarios, with **fixed tiny amounts** (0.01 USDT / 0.01 MNT). Recipients: a few
   throwaway testnet addresses (to verify recipient handling) + the hot wallet itself
   (self-send) for volume.
3. Builds the **real** `PayoutExecutor` with `build_chain_client` (BSC testnet: RPC
   `bsc-testnet.publicnode.com` + fallback, chainId 97, USDT contract above) and
   `build_mantle_chain_client` (Mantle Sepolia RPC `rpc.sepolia.mantle.xyz`, chainId
   5003), key from `.env`, `confirmations=1`, `auto_limit` low enough that some USDT
   payouts route to `pending_admin`.
4. Runs the full executor state machine (`process_next` + `poll_confirmations`, plus an
   admin-release step for `pending_admin`) until every payout is terminal.
5. Verifies each payout on-chain + in the DB (txhash, status paid/failed, per-chain nonce)
   and prints explorer links.
6. `DROP DATABASE simtest`.

Budget: 30 × 0.01 = 0.3 USDT worst case (self-sends cost 0); well within 3.67. Time:
~30 sequential sends at confirmations=1 ≈ a few minutes.

## Scenario catalog (Part A)

Grouped; ✚ marks scenarios NEW vs the prior harness (which already covered ranks,
USDT/MNT withdrawals, pending_admin, cooldown, min-gate, one-active-payout, override,
pause, block).

**Accrual / ranks:** tiers 0–4; intra-month rank progression; carry-over floor (a rank
protects the next 2 months); multi-account users; MNT dual-asset accrual valued in
USDT-equiv; 30-day lifecycle available→expired; sub-$10 dust months (non-withdrawable,
expire on schedule); ✚ rank-4 spike month.

**Withdrawals:** USDT auto-send (<limit); large USDT (>limit)→pending_admin→admin release;
MNT→always pending_admin→admin release; cooldown (30/14/7/1 days by rank); min-payout gate;
one-active-payout-per-user across both assets; gas-exceeds-net path.

**Admin:** kill-switch pause/resume; block/unblock (a blocked user's reserved payout fails
and returns funds); ✚ reassign UID (dispute); ✚ block UID; rate-by-nickname override +
current-month recompute; ✚ reduce-commission on a pending payout; stats
(earned/rebate/profit total + month).

**Executor / resilience:** ✚ reverted tx → payout failed → accruals returned to available;
blocked user mid-flight → payout fails; ✚ exchange-adapter failure for one user for a cycle
(dev alert, other users unaffected, retried next cycle); ✚ MNT-rate fetch failure → whole
`_process_user` skipped for Bybit-holding users that cycle (no rank corruption, no loss);
✚ downtime (skipped cycles → gap > threshold → expiry grace recorded); idempotency
(re-running a cycle re-freezes nothing already finalized); ✚ new user + account added
mid-sim (accrual_since honored); ✚ UID collision (two users claim same uid → first-come).

Scenario events are injected on scheduled sim-days by small injector functions; the day
loop otherwise runs users through the real services so most scenarios emerge naturally.

## Invariants (Part A)

Keep the prior 13 (per-accrual money identity `amount = net + commission`; no negative
money; `earned = rebate + profit`; sent/paid payouts have txhash+nonce; ≤1 active payout
per user; MNT sent/paid were admin-approved; paid accruals belong to paid payouts; no
available accrual past expiry; reserved accruals linked to an active payout; paid payouts
respect ≥1-day min cooldown; rate override recomputed current month; pause blocks
withdrawal; block blocks withdrawal). ADD:

- A failed payout returned its accruals to `available` (none stuck `reserved` on a
  `failed`/`cancelled` payout).
- A blocked user's in-flight/reserved payout ended `failed`, funds back to `available`.
- After UID reassignment, future accruals for that `(exchange, uid)` are attributed to the
  new owner; already-frozen rows keep their original owner.
- A reduce-commission raised the payout's `net_usdt` (never lowered) and never exceeds
  `gross`.
- The adapter-failure user still has accruals frozen for their OTHER (non-failing) days /
  next cycle; no other user's cycle was rolled back.
- The MNT-rate-failure cycle left the affected users' days unfinalized (retried next cycle)
  and corrupted no rank (no USDT-only freeze on a day that should carry MNT).
- Total realism guard: max per-user cumulative rebate ≤ ~$20k and total ≤ ~$300k (catches
  accidental whale reintroduction).

## Report

Part A: ranks distribution (latest closed month), accrual state×asset counts/sums, payouts
by status×asset, scenario-event outcomes (override before/after, pause/block/reassign/
block-uid/reduce results), earnings snapshot (earned/rebate/profit total+month), and the
invariant PASS/FAIL list. Part B: a table of the ~30 real sends — asset, amount, recipient
(masked), nonce, status, txhash, explorer link — plus per-chain nonce continuity and a
final PASS/FAIL.

## Testing / success criteria

- Part A: the harness IS the test — all invariants PASS on a full 100-day run; the report
  shows the designed scenarios actually occurred (non-zero counts for each injected event).
- Part B: every real send reaches its expected terminal state (success for normal sends;
  `failed`/`below_min` for the negative case), verified in the `simtest` DB and on the
  block explorers; `simtest` is dropped afterward; prod `bot` DB untouched.
- Existing pytest suite stays green (the harness lives under `sim/`, not `tests/`).

## Out of scope (YAGNI)

- Real reverted-tx on testnet (can't force a real revert cheaply) — revert is covered in
  Part A via the mock chain; Part B's negative case is `below_min`.
- Sending real testnet funds to all 50 users at sim-scale (budget) — Part B is bounded.
- Any change to production code behavior; the sim only drives existing code paths.
- Touching the mainnet payout path or prod `bot` DB.

## Security notes

- Prod key never extracted/printed; real sends run in-container on the server.
- `simtest` DB is isolated and dropped; prod `bot` DB never opened by the sim.
- Recipient addresses in Part B are throwaway testnet addresses or self-send.
- No secrets in the report (addresses masked, no keys/tokens).
