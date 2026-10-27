# Phase 5 — Payout-executor Implementation Plan (chain-agnostic, mock-only)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Drive `payouts(status='reserved')` (produced by Phase 4) to `paid` (or `failed` / `pending_admin`) via a chain-agnostic executor: single-flight nonce, idempotency, "sent-but-unknown" safety, 15-confirmation finalize, startup reconciliation — all against a `MockChainClient` (real web3 client + keys + RPC are added at deploy).

**Architecture:** New `bot/executor/` package. `chain.py` defines a `ChainClient` Protocol + a deterministic `MockChainClient` + the money-critical `usdt_to_wei` (18 decimals). `repository.py` does atomic payout state transitions + accrual finalization. `executor.py` is the state-machine driver (process_next / poll_confirmations / reconcile). `scheduler.py` loops it. No real blockchain library is added in this phase.

**Tech Stack:** Python 3.11+, SQLAlchemy 2.0 async + Postgres (testcontainers), Decimal-only money, pytest-asyncio (`loop_scope="session"`), cryptography.Fernet (reuse `SecretCipher` to decrypt the recipient address).

## Global Constraints

- **NO mainnet, NO real private keys, NO real funds, NO new blockchain dependency.** Executor runs on `MockChainClient` only. The recipient address is decrypted from `payouts.address_encrypted` via `SecretCipher`.
- **Money is `Decimal`; on-chain amount is integer wei with `decimals = 18` (BEP20 USDT on BSC — NOT 6).** `usdt_to_wei` rounds DOWN to 2 dp (business rule) then ×10^18. An error here is a 10^12× mispayment — test exhaustively.
- **Single-flight:** at most ONE payout in flight (`submitting`/`sent`) at a time. `nonce = max(on-chain nonce, last_db_nonce + 1)`, persisted on the payout row BEFORE broadcast.
- **Never re-send blindly (§4.3):** on RPC uncertainty, check the chain first (by txhash, else by nonce from our address). A second broadcast is only a *replacement* (same nonce), never a new payout. `idempotency_key` is unique (already in schema).
- **Confirmations:** `sent → confirmed` only at ≥ `CONFIRMATIONS` (=15); `confirmed → paid` finalizes. Never `paid` straight after broadcast.
- **Reconcile on startup:** every `submitting`/`sent` payout is resolved against the chain before anything new is sent.
- **Accrual side-effects (atomic, in the same tx as the payout transition):** `paid` → its accruals `reserved → paid`; `failed` → its accruals `reserved → available` and `payout_id` cleared (returned to the live pool, never lost, never double-sent).
- **Cooldown anchors to a successful payout** (`confirmed_at`) — consumed by Phase 4's `last_success_withdrawal_at`.
- **Over `AUTO_LIMIT` ($200):** `reserved → pending_admin`, executor does NOT send (admin release is Phase 6).
- **Gas (test phase):** NOT deducted from the user (owner bears it); `MAX_GAS_PRICE`/BNB rate deferred. `net_usdt` stays as Phase 4 computed it.
- **Expiry paused for `reserved`/`pending_admin`:** `bot.accrual.repository.expire_aged` already only touches `available` — add a regression test asserting a reserved accrual is NOT expired by age.
- Every state transition is one DB transaction + an `audit_events` row (actor="executor", txid/nonce/amount). No secrets in logs (never log the decrypted address or key).
- Gate: `ruff` + `ruff format` + `mypy bot` clean on new files (repo baseline RUF001 Cyrillic / 24 mypy pre-existing in OTHER files — add none); full `pytest` green. venv binaries: `./.venv/Scripts/{pytest,ruff,mypy}.exe`.

## File Structure
- `bot/executor/__init__.py`, `chain.py`, `repository.py`, `executor.py`, `scheduler.py` (Create)
- `bot/config.py` (Modify) — `AUTO_LIMIT_USDT`, `CONFIRMATIONS`, `payout_wallet_address`, `bsc_rpc_urls` (list; unused by mock but wired for the real client), `usdt_contract`.
- `tests/executor/…` (Create) — Postgres fixtures copied from `tests/accrual/conftest.py`.

---

## Task 1: `usdt_to_wei` + ChainClient Protocol + MockChainClient

**Files:** Create `bot/executor/__init__.py` (empty), `bot/executor/chain.py`, `tests/executor/__init__.py` (empty), `tests/executor/conftest.py` (copy `tests/accrual/conftest.py`), `tests/executor/test_chain.py`.

**Interfaces:**
- `usdt_to_wei(amount: Decimal) -> int`
- `class ChainError(Exception)` — RPC/broadcast uncertainty.
- `class ChainClient(Protocol)`:
  - `async def get_nonce(self, address: str) -> int`
  - `async def send_transfer(self, *, to: str, amount_wei: int, nonce: int) -> str` (returns txhash; may raise `ChainError`)
  - `async def get_tx_status(self, txhash: str) -> str` → `"pending"|"success"|"failed"|"unknown"`
  - `async def get_confirmations(self, txhash: str) -> int`
  - `async def find_tx_by_nonce(self, address: str, nonce: int) -> str | None` (txhash of a known tx from `address` with `nonce`, else None)
- `class MockChainClient` — programmable, deterministic; implements the protocol.

- [ ] **Step 1: Write failing tests**

```python
# tests/executor/test_chain.py
from __future__ import annotations

from decimal import Decimal

import pytest

from bot.executor.chain import ChainError, MockChainClient, usdt_to_wei


def test_usdt_to_wei_18_decimals_not_6():
    assert usdt_to_wei(Decimal("1")) == 10**18
    assert usdt_to_wei(Decimal("0.01")) == 10**16
    assert usdt_to_wei(Decimal("45")) == 45 * 10**18


def test_usdt_to_wei_rounds_down_to_cents():
    # business rule: floor to 2dp, THEN scale
    assert usdt_to_wei(Decimal("1.239")) == 123 * 10**16      # 1.23
    assert usdt_to_wei(Decimal("0.019")) == 1 * 10**16        # 0.01


@pytest.mark.asyncio(loop_scope="session")
async def test_mock_send_records_and_reports():
    chain = MockChainClient(nonce=7)
    assert await chain.get_nonce("0xwallet") == 7
    txh = await chain.send_transfer(to="0xrecipient", amount_wei=10**18, nonce=7)
    assert await chain.get_tx_status(txh) == "success"
    assert await chain.find_tx_by_nonce("0xwallet", 7) == txh


@pytest.mark.asyncio(loop_scope="session")
async def test_mock_can_simulate_broadcast_uncertainty():
    # send raises ChainError, but the tx still landed on-chain under the nonce
    chain = MockChainClient(nonce=7, fail_send_but_land=True)
    with pytest.raises(ChainError):
        await chain.send_transfer(to="0xr", amount_wei=10**18, nonce=7)
    # reconcile-by-nonce finds it => proves "sent but unknown" is recoverable
    assert await chain.find_tx_by_nonce("0xwallet", 7) is not None
```

- [ ] **Step 2: Run — expect fail.** `./.venv/Scripts/pytest.exe tests/executor/test_chain.py -v`

- [ ] **Step 3: Implement `bot/executor/chain.py`**

```python
# bot/executor/chain.py
"""Chain abstraction for the payout executor. Real web3/RPC lives behind ChainClient
and is added at deploy; this module ships only the money-math and a deterministic mock.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Protocol

_WEI = Decimal(10) ** 18
_CENT = Decimal("0.01")


def usdt_to_wei(amount: Decimal) -> int:
    """USDT (Decimal) -> integer wei at 18 decimals, floored to whole cents first.

    BEP20 USDT on BSC has 18 decimals (NOT 6). Business rule rounds payouts down to
    the cent; we then scale by 10**18.
    """
    cents = amount.quantize(_CENT, rounding=ROUND_DOWN)
    return int(cents * _WEI)


class ChainError(Exception):
    """Broadcast/RPC uncertainty — the caller must NOT blindly re-send (see §4.3)."""


class ChainClient(Protocol):
    async def get_nonce(self, address: str) -> int: ...
    async def send_transfer(self, *, to: str, amount_wei: int, nonce: int) -> str: ...
    async def get_tx_status(self, txhash: str) -> str: ...
    async def get_confirmations(self, txhash: str) -> int: ...
    async def find_tx_by_nonce(self, address: str, nonce: int) -> str | None: ...


class _Tx:
    __slots__ = ("txhash", "nonce", "status", "confirmations")

    def __init__(self, txhash: str, nonce: int, status: str, confirmations: int) -> None:
        self.txhash = txhash
        self.nonce = nonce
        self.status = status
        self.confirmations = confirmations


class MockChainClient:
    """Deterministic in-memory chain for tests. Programmable per-test.

    address is fixed as the sending wallet ("0xwallet"); txhashes are derived from nonce
    so they're stable without randomness (Math.random/uuid are unavailable in this env
    and tests must be reproducible).
    """

    WALLET = "0xwallet"

    def __init__(
        self,
        *,
        nonce: int = 0,
        fail_send_but_land: bool = False,
        fail_send_no_land: bool = False,
        default_confirmations: int = 15,
        default_status: str = "success",
    ) -> None:
        self._nonce = nonce
        self._fail_send_but_land = fail_send_but_land
        self._fail_send_no_land = fail_send_no_land
        self._default_confirmations = default_confirmations
        self._default_status = default_status
        self._by_hash: dict[str, _Tx] = {}
        self._by_nonce: dict[int, _Tx] = {}

    @staticmethod
    def _hash_for(nonce: int) -> str:
        return f"0xtx{nonce:064x}"

    async def get_nonce(self, address: str) -> int:
        return self._nonce

    async def send_transfer(self, *, to: str, amount_wei: int, nonce: int) -> str:
        txh = self._hash_for(nonce)
        tx = _Tx(txh, nonce, self._default_status, self._default_confirmations)
        if self._fail_send_no_land:
            raise ChainError("broadcast failed, nothing landed")
        # record the tx (it "landed") BEFORE optionally raising, so reconcile can find it
        self._by_hash[txh] = tx
        self._by_nonce[nonce] = tx
        if self._fail_send_but_land:
            raise ChainError("RPC timeout after broadcast; status unknown")
        return txh

    async def get_tx_status(self, txhash: str) -> str:
        tx = self._by_hash.get(txhash)
        return tx.status if tx else "unknown"

    async def get_confirmations(self, txhash: str) -> int:
        tx = self._by_hash.get(txhash)
        return tx.confirmations if tx else 0

    async def find_tx_by_nonce(self, address: str, nonce: int) -> str | None:
        tx = self._by_nonce.get(nonce)
        return tx.txhash if tx else None

    # --- test helpers ---
    def set_confirmations(self, nonce: int, n: int) -> None:
        self._by_nonce[nonce].confirmations = n

    def set_status(self, nonce: int, status: str) -> None:
        self._by_nonce[nonce].status = status
```

- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: gate + commit** (`ruff`/`format`/`mypy`, `feat(executor): usdt_to_wei (18dp) + ChainClient protocol + MockChainClient`).

---

## Task 2: Executor repository (atomic state transitions)

**Files:** Create `bot/executor/repository.py`, `tests/executor/test_repository.py`.

**Interfaces (all `async`, `session` first):**
- `payouts_in_flight(session) -> list[Payout]` — status in (`submitting`,`sent`).
- `claim_next_reserved(session) -> Payout | None` — oldest `reserved` by `requested_at`, `FOR UPDATE SKIP LOCKED`, limit 1.
- `last_nonce(session) -> int | None` — max `nonce` over all payouts.
- `mark_pending_admin(session, payout_id) -> None` — `reserved → pending_admin`.
- `mark_submitting(session, payout_id, nonce: int) -> None` — `reserved → submitting`, set `nonce`.
- `mark_sent(session, payout_id, txhash: str, now) -> None` — `submitting → sent`, set `txhash`, `sent_at=now`.
- `finalize_paid(session, payout_id, now) -> None` — `→ paid`, `confirmed_at`/`paid_at=now`; `UPDATE rebate_accruals SET state='paid' WHERE payout_id=:id AND state='reserved'`.
- `finalize_failed(session, payout_id, reason: str) -> None` — `→ failed`, `failed_reason=reason`; `UPDATE rebate_accruals SET state='available', payout_id=NULL WHERE payout_id=:id AND state='reserved'`.
- `write_audit(...)` — reuse `bot.botui.repository.write_audit` (import it) rather than duplicate.

- [ ] **Step 1** Write failing tests (real Postgres; seed a user+wallet+reserved payout+reserved accruals via a helper mirroring `tests/botui/test_withdraw.py`):
  - `claim_next_reserved` returns the oldest reserved and (with SKIP LOCKED) a second concurrent claim in another session returns a different/none.
  - `finalize_paid` flips the payout to `paid` AND its accruals `reserved → paid`.
  - `finalize_failed` flips to `failed` AND accruals back to `available` with `payout_id` NULL.
  - `last_nonce` returns the max nonce.
  - `payouts_in_flight` returns only submitting/sent.
- [ ] **Step 2** Run — expect fail.
- [ ] **Step 3** Implement (use `select(...).with_for_update(skip_locked=True)`; `update(...)` for accrual side-effects; `typing.cast` for scalars/rowcount as elsewhere).
- [ ] **Step 4** Run — expect pass.
- [ ] **Step 5** gate + commit (`feat(executor): atomic payout transitions + accrual finalization`).

---

## Task 3: `executor.process_next` (reserved → submitting → sent)

**Files:** Create `bot/executor/executor.py`, `tests/executor/test_process_next.py`.

**Interfaces:**
- `class PayoutExecutor.__init__(self, sessionmaker, chain: ChainClient, cipher: SecretCipher, *, now: Callable[[], datetime], wallet_address: str, auto_limit: Decimal, min_payout: Decimal, confirmations: int)`
- `async def process_next(self) -> None`

**Algorithm (single-flight):**
1. If `payouts_in_flight(session)` is non-empty → return (let `poll_confirmations` drive it; never start a second).
2. `payout = claim_next_reserved(session)`; if None → return.
3. `net = payout.net_usdt`. If `net > auto_limit` → `mark_pending_admin`; audit; return. If `net < min_payout` or `net <= 0` → `finalize_failed("below_min")`; audit; return.
4. `nonce = max(await chain.get_nonce(wallet_address), (last_nonce(session) or -1) + 1)`.
5. `mark_submitting(payout.id, nonce)`; **commit/flush BEFORE broadcast** (nonce persisted first, §4.3.1).
6. `to = cipher.decrypt(payout.address_encrypted.decode())`; `amount_wei = usdt_to_wei(net)`.
7. `try: txhash = await chain.send_transfer(to=to, amount_wei=amount_wei, nonce=nonce)` — on `ChainError`: leave in `submitting` (reconcile resolves it), audit "broadcast_uncertain", return. Do NOT re-send here.
8. `mark_sent(payout.id, txhash, now)`; audit "sent" (txid, nonce).

- [ ] **Step 1** Write failing tests (real Postgres + MockChainClient):
  - reserved ≤ limit → after `process_next`, payout is `sent`, nonce persisted, txhash set, `to` == decrypted address, `amount_wei == usdt_to_wei(net)`.
  - `net > 200` → `pending_admin`, nothing sent.
  - single-flight: with an existing `sent` payout, `process_next` does not start another.
  - broadcast `ChainError` (fail_send_no_land) → payout stays `submitting`, no second send.
- [ ] **Step 2** Run — expect fail.
- [ ] **Step 3** Implement (nonce persisted before broadcast; decrypt recipient; Decimal money).
- [ ] **Step 4** Run — expect pass.
- [ ] **Step 5** gate + commit (`feat(executor): process_next — single-flight submit with nonce-before-broadcast`).

---

## Task 4: `poll_confirmations` (sent → confirmed → paid)

**Files:** Extend `bot/executor/executor.py`, create `tests/executor/test_confirmations.py`.

**Interfaces:** `async def poll_confirmations(self) -> None`

**Algorithm:** for each `sent` payout (there is at most one, single-flight):
- `st = await chain.get_tx_status(payout.txhash)`.
- `st == "failed"` → `finalize_failed("reverted")`; audit.
- `st == "success"` and `await chain.get_confirmations(payout.txhash) >= confirmations` → `finalize_paid(payout.id, now)` (accruals → paid, `confirmed_at`/`paid_at`); audit "paid".
- else (pending / not enough confs) → leave as `sent`.

- [ ] **Step 1** Write failing tests:
  - `sent` with < 15 confs → stays `sent`, accruals stay `reserved`.
  - `sent` with ≥ 15 confs, status success → `paid`, accruals `paid`, `paid_at`/`confirmed_at` set.
  - a subsequent `bot.botui.repository.last_success_withdrawal_at` returns this payout's time (cooldown anchor works end-to-end).
  - reverted tx → `failed`, accruals back to `available`.
- [ ] **Step 2** Run — expect fail.
- [ ] **Step 3** Implement.
- [ ] **Step 4** Run — expect pass.
- [ ] **Step 5** gate + commit (`feat(executor): confirmation polling — sent->confirmed(15)->paid`).

---

## Task 5: "Sent but unknown" + startup reconcile (no blind re-send)

**Files:** Extend `bot/executor/executor.py`, create `tests/executor/test_reconcile.py`.

**Interfaces:** `async def reconcile(self) -> None`

**Algorithm (startup, §6.1) — for each payout in (`submitting`,`sent`):**
- `submitting` (broadcast outcome unknown): `txh = payout.txhash or await chain.find_tx_by_nonce(wallet_address, payout.nonce)`. If found → `mark_sent(payout.id, txh, now)` (it DID land — never send a second). If not found → the tx never landed; it is safe to resubmit on the SAME nonce next `process_next` — leave `submitting` but audit "reconcile_not_found" (a follow-up may auto-resubmit; do not do it blindly inside reconcile).
- `sent`: run the same status/confirmation check as `poll_confirmations` (drive to paid/failed).

- [ ] **Step 1** Write failing tests (the core money-safety proof):
  - **`test_sent_but_unknown_never_double_sends`**: `process_next` with `MockChainClient(fail_send_but_land=True)` → payout left `submitting`, tx DID land under the nonce. Then `reconcile()` → finds it by nonce → `sent` → (confs) → `paid`. Assert exactly ONE tx exists for that nonce and the payout is `paid` — no second broadcast.
  - `submitting` where the tx did NOT land (`fail_send_no_land`) → `reconcile` leaves it `submitting` (recoverable), does not fabricate a paid.
  - `reconcile` drives an already-`sent` payout with 15 confs to `paid`.
- [ ] **Step 2** Run — expect fail.
- [ ] **Step 3** Implement.
- [ ] **Step 4** Run — expect pass.
- [ ] **Step 5** gate + commit (`feat(executor): startup reconcile + sent-but-unknown recovery (no blind resend)`).

---

## Task 6: Expiry-pause regression + audit coverage

**Files:** `tests/executor/test_invariants.py` (Create); touch `bot/executor/*` only if a test finds a real defect.

- [ ] **Step 1** Write tests:
  - **expiry pause:** seed a `reserved` accrual whose `expires_at` is in the past; call `bot.accrual.repository.expire_aged(now_after)`; assert it stays `reserved` (only `available` expires). And a `pending_admin` payout's accruals likewise untouched.
  - **audit:** after a full reserved→…→paid run, `audit_events` contains the executor's `sent` and `paid` rows with the txid/nonce/amount; after a failed run, a `failed` audit row.
- [ ] **Step 2** Run. If the expiry-pause test fails, that's a real money bug — fix `bot/accrual/repository.py` (should already be correct: `expire_aged` filters `state='available'`) and report.
- [ ] **Step 3** gate + commit (`test(executor): expiry-pause for reserved + audit-trail coverage`).

---

## Task 7: Scheduler, config, quality gate

**Files:** Create `bot/executor/scheduler.py`; modify `bot/config.py`; `tests/executor/test_scheduler.py`; update `autonomous_todo.md`, ledger.

**Interfaces:**
- `bot/config.py`: `auto_limit_usdt: Decimal = Decimal("200")` (alias `AUTO_LIMIT_USDT`), `confirmations: int = 15` (`CONFIRMATIONS`), `payout_wallet_address: str | None` (`PAYOUT_WALLET_ADDRESS`), `bsc_rpc_urls_raw: str = ""` (`BSC_RPC_URLS`, comma-separated; parsed to a list; unused by the mock, wired for the real client), `usdt_contract: str | None` (`USDT_CONTRACT`).
- `class ExecutorScheduler.__init__(self, executor: PayoutExecutor, *, sleep, now, interval_seconds=30.0)`; `async def run_forever(self, max_cycles=None) -> int` — first `reconcile()`, then each cycle `process_next()` + `poll_confirmations()`; exceptions logged (structlog) and swallowed so the loop survives; returns cycles run.

- [ ] **Step 1** Failing scheduler test (a `_CountingExecutor` stub with `reconcile`/`process_next`/`poll_confirmations` coroutines + fake sleep): `run_forever(max_cycles=3)` calls `reconcile` once then process/poll 3× each; a raising cycle doesn't stop the loop. Config test: `AUTO_LIMIT_USDT`/`CONFIRMATIONS` parse from env with defaults.
- [ ] **Step 2** Run — expect fail.
- [ ] **Step 3** Implement scheduler + config fields.
- [ ] **Step 4** Full gate: `ruff check`/`format`/`mypy bot` clean (new files); `./.venv/Scripts/pytest.exe -q` green. Update `autonomous_todo.md` (Phase 5 done + note: real ChainClient/keys/RPC + gas-deduction are deploy-time; pending_admin release is Phase 6) and the ledger.
- [ ] **Step 5** Commit (`feat(executor): scheduler + config (AUTO_LIMIT/CONFIRMATIONS/RPC urls)`).

---

## Documented assumptions (owner: review)
1. **Chain-agnostic; only `MockChainClient` in this phase.** Real web3 client, payout-wallet private key, and RPC URLs are deploy-time — no blockchain dependency added here. `BSC_RPC_URLS` is a comma-separated env list (public BSC endpoints need no key; a keyed provider URL is optional for reliability).
2. **AUTO_LIMIT = $200** → `pending_admin`; admin release is Phase 6.
3. **Gas not deducted (test phase)** — owner bears it; revert to per-user gas deduction when a BNB→USDT rate + `MAX_GAS_PRICE` are wired.
4. **Payout notification (txid) to the user** — interface only; auto-send needs a live Bot (deploy/integration); mocked in tests.
5. **Outsourced signer/HSM** (§2) — not in this phase (no key here at all).

## Self-Review (author)
- Spec coverage: single-flight+nonce (§4.2), sent-but-unknown+reconcile (§4.3/§6.1), 15 confs (§4.5), decimals=18 (§4.6), AUTO_LIMIT/pending_admin (§8), accrual finalize paid/failed, cooldown anchor, expiry-pause (§6.2), audit (§1) — all mapped. Gas/MAX_GAS_PRICE/real-signer explicitly deferred per §10.
- Complete code for the money-critical Task 1 (usdt_to_wei + mock) and precise algorithms + concrete tests for 2-7. No new dependency.
- Names consistent across tasks: `ChainClient`/`MockChainClient`/`ChainError`/`usdt_to_wei`, `PayoutExecutor.{process_next,poll_confirmations,reconcile}`, repository transition names; reuse `bot.botui.repository.write_audit` + `bot.accrual.repository.expire_aged`.
