# Phase 6 — Admin / roles Implementation Plan (final)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Admin controls in the same bot (role-gated): persistent per-user commission override (only-down, from a date), release/reject of >$200 (`pending_admin`) payouts, UID dispute resolution, user block/unblock, and a payouts kill-switch — plus the accrual-worker integration that makes the commission override actually lower frozen fees.

**Architecture:** New `bot/admin/` package (router + repository + service), gated by an `IsAdmin` filter. A pure `effective_fee` lives in `bot/payout/`; the merged accrual-worker calls it at freeze time using each user's override. Kill-switch/blocked checks thread into `executor.process_next` and `botui.withdraw`. Money-touching logic is verified on real Postgres.

**Tech Stack:** aiogram 3.13.1, SQLAlchemy 2.0 async + Postgres (testcontainers) + Alembic, Decimal-only money, pytest-asyncio (`loop_scope="session"`).

## Global Constraints
- **Money is `Decimal`.** Commission override is **only-down**: effective day fee = `min(rank_fee, override_pct)`. Applies to accruals **frozen on/after** `commission_override_since` (past frozen rows never change — freeze rule holds). `0 <= override_pct <= 1`.
- **Admin gating:** admin-only handlers check `users.role == 'admin'` (from `event.from_user.id`, never from callback-data). Whitelist stays global; admin is an extra gate.
- **Release >$200:** `pending_admin → reserved` (executor picks it up); reject → `cancelled` + accruals `reserved → available` (payout_id cleared).
- **Kill-switch:** `system_flags['payouts_paused'] = {"paused": bool}`. When paused, `executor.process_next` sends nothing and `botui.withdraw.prepare_withdrawal` blocks with a message. Blocked users (`status='blocked'`) cannot withdraw/add.
- **Every money/state action writes an `audit_events` row with a reason** (actor = admin telegram id).
- **First admin** is bootstrapped from env (`ADMIN_TELEGRAM_IDS`, comma-separated) at `upsert_user` time — not assignable in-bot (no one to grant the first).
- `ruff`+`format`+`mypy bot` clean on new files (RUF001 Cyrillic in Russian UI is the tolerated repo baseline; no other findings; no new mypy errors beyond the ~24 pre-existing). Full `pytest` green. venv: `./.venv/Scripts/{pytest,ruff,mypy}.exe`. Docker for Postgres tests.

## File Structure
- `bot/payout/commission.py` (Modify) — add `effective_fee`.
- `bot/db/models/identity.py` (Modify) — add `commission_override_pct`, `commission_override_since` to `User`.
- `alembic/versions/<new>.py` (Create) — migration for the two columns.
- `bot/accrual/repository.py` (Modify) — `get_commission_override(session, user_id)`.
- `bot/accrual/worker.py` (Modify) — apply `effective_fee` at freeze.
- `bot/admin/__init__.py`, `filters.py`, `repository.py`, `service.py`, `router.py`, `keyboards.py` (Create).
- `bot/config.py` (Modify) — `admin_telegram_ids`.
- `bot/executor/executor.py`, `bot/botui/withdraw.py`, `bot/botui/repository.py` (Modify) — kill-switch/blocked checks.
- `bot/main.py` (Modify) — register admin router; admin bootstrap.
- `tests/…` mirrors.

---

## Task 1: Migration + `effective_fee`

**Files:** Modify `bot/payout/commission.py`, `bot/db/models/identity.py`; Create migration; Test `tests/payout/test_effective_fee.py`, `tests/db/test_migration_override.py`.

**Interfaces:** `effective_fee(rank: int, override_pct: Decimal | None) -> Decimal` — `base = fee_pct(rank)`; return `base` if `override_pct is None` else `min(base, override_pct)`. (Caller guarantees `rank >= 1`.)

- [ ] **Step 1: Write failing tests**

```python
# tests/payout/test_effective_fee.py
from __future__ import annotations
from decimal import Decimal
import pytest
from bot.payout.commission import effective_fee

D = Decimal


def test_no_override_uses_rank_fee():
    assert effective_fee(1, None) == D("0.10")
    assert effective_fee(2, None) == D("0.07")


def test_override_only_lowers():
    assert effective_fee(1, D("0.05")) == D("0.05")      # 5% < 10% rank -> 5%
    assert effective_fee(1, D("0.20")) == D("0.10")      # 20% > 10% -> capped at rank (only down)
    assert effective_fee(4, D("0.05")) == D("0")         # rank4 fee 0 -> min stays 0
```

- [ ] **Step 2: Run — expect fail.** `./.venv/Scripts/pytest.exe tests/payout/test_effective_fee.py -v`

- [ ] **Step 3: Implement** in `bot/payout/commission.py`:

```python
from bot.payout.ranks import fee_pct, is_payable  # is_payable may already be imported


def effective_fee(rank: int, override_pct: Decimal | None) -> Decimal:
    """Frozen fee for a payable rank, lowered by an optional per-user override.

    Only-down: the override can reduce the fee (user gets more) but never raise it
    above the rank fee. Caller guarantees rank >= 1. See admin design (B)."""
    base = fee_pct(rank)
    if override_pct is None:
        return base
    return min(base, override_pct)
```

- [ ] **Step 4: Add model columns** to `User` in `bot/db/models/identity.py`:

```python
    commission_override_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    commission_override_since: Mapped[date | None] = mapped_column(Date)
```
(Add `Numeric`, `Date`, `Decimal`, `date` imports if missing.)

- [ ] **Step 5: Alembic migration** — generate/write a migration adding both nullable columns to `users`; downgrade drops them. Run the migration round-trip test style used in `tests/db/test_migrations.py` (upgrade on a fresh testcontainer, assert columns exist; downgrade removes them). Follow the existing migration file conventions (they're `extend-exclude`d from ruff).

- [ ] **Step 6:** Run `./.venv/Scripts/pytest.exe tests/payout/test_effective_fee.py tests/db -v` — pass. Gate + commit (`feat(admin): effective_fee (only-down) + users commission-override columns + migration`).

---

## Task 2: Accrual-worker integration (override lowers frozen fee from `since`)

**Files:** Modify `bot/accrual/repository.py`, `bot/accrual/worker.py`; Test `tests/accrual/test_override_fee.py`.

**Interfaces:** `get_commission_override(session, user_id) -> tuple[Decimal | None, date | None]` — returns `(commission_override_pct, commission_override_since)` for the user.

- [ ] **Step 1: Write failing tests** (real Postgres + fake adapter, mirror `tests/accrual/test_worker.py`):
  - Set a user's `commission_override_pct=0.02`, `commission_override_since=2026-07-12`. Accrue July 10-15 (rank 1). Assert days ≥ 2026-07-12 froze `fee_pct=0.02` (min(0.10, 0.02)); days < 2026-07-12 froze `fee_pct=0.10`. Commissions computed with the frozen fee.
  - Override `0.20` (above rank) → frozen fee stays `0.10` (only-down).
  - No override → `0.10` as before (regression).

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement.** Add to `bot/accrual/repository.py`:

```python
async def get_commission_override(
    session: AsyncSession, user_id: int
) -> tuple[Decimal | None, date | None]:
    stmt = select(User.commission_override_pct, User.commission_override_since).where(
        User.id == user_id
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None, None
    return row[0], row[1]
```
(Import `User` from `bot.db.models.identity`.)

In `bot/accrual/worker.py::_process_user`, read the override once near the top (after resolving `accounts`, before the day loop):

```python
        override_pct, override_since = await repo.get_commission_override(session, user_id)
```
Replace the fee line (currently `fee = fee_pct(rank)`) in the per-account loop with:

```python
                if rank >= 1:
                    day_override = (
                        override_pct
                        if (override_since is not None and override_pct is not None and d >= override_since)
                        else None
                    )
                    fee = effective_fee(rank, day_override)
                    commission = amount * fee
                    net = amount - commission
                else:
                    fee = Decimal(0)
                    commission = Decimal(0)
                    net = amount
```
Import `effective_fee` from `bot.payout.commission`. (`d` is the loop day; `override_since` gates it — pre-`since` days and backfill of old days keep the rank fee.)

- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5:** Full suite + gate + commit (`feat(admin): accrual-worker applies per-user commission override at freeze (only-down, from since)`).

---

## Task 3: `IsAdmin` filter, admin repository, `/admin` menu, kill-switch flag

**Files:** Create `bot/admin/__init__.py`, `filters.py`, `repository.py`, `keyboards.py`, `router.py` (menu only here); Modify `bot/config.py`; Test `tests/admin/…`.

**Interfaces:**
- `config`: `admin_telegram_ids_raw: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")` + `@cached_property admin_telegram_ids -> frozenset[int]` (parse like `user_telegram_ids`).
- `bot/admin/filters.py`: `class IsAdmin(BaseFilter)` — `async def __call__(self, event, session) -> bool`: load the user, return `user is not None and user.role == "admin"`. (Gets `session` from middleware data.)
- `bot/admin/repository.py` (async): `get_flag(session, key) -> dict | None`, `set_flag(session, key, value: dict, by: int)` (upsert `system_flags`), `is_payouts_paused(session) -> bool`, `list_pending_admin(session) -> list[Payout]`, `set_user_status(session, user_id, status)`, `set_commission_override(session, user_id, pct, since, by)`, `clear_commission_override(session, user_id, by)`, plus reuse `bot.botui.repository.write_audit`.
- `bot/admin/router.py`: `@router.message(Command("admin"), IsAdmin())` → admin menu keyboard.

- [ ] **Step 1** Failing tests: `IsAdmin` true for admin / false for user (real Postgres, seed users); config parses `ADMIN_TELEGRAM_IDS`; `set_flag`/`is_payouts_paused` round-trip; `list_pending_admin` returns only `pending_admin`.
- [ ] **Step 2** Run — expect fail.
- [ ] **Step 3** Implement config field, `filters.py`, `repository.py`, `keyboards.py` (admin menu `AdminCB`), `router.py` menu handler. `set_flag` uses `pg_insert(...).on_conflict_do_update` on `system_flags.key`.
- [ ] **Step 4** Run — expect pass. Gate + commit (`feat(admin): IsAdmin filter, admin repository, /admin menu, kill-switch flag`).

---

## Task 4: Release / reject `pending_admin` payouts (+ one-time commission reduction)

**Files:** Extend `bot/admin/repository.py`, `service.py`, `router.py`, `keyboards.py`; Test `tests/admin/test_release.py`.

**Interfaces:**
- `repo.release_payout(session, payout_id) -> bool` — `pending_admin → reserved` (executor will pick up); returns changed.
- `repo.reject_payout(session, payout_id) -> bool` — `pending_admin → cancelled` + `UPDATE rebate_accruals SET state='available', payout_id=NULL WHERE payout_id=:id AND state='reserved'`.
- `repo.reduce_payout_commission(session, payout_id, new_commission: Decimal, by, reason) -> bool` — only-down (`new_commission <= commission_usdt`), set `commission_override_usdt`, `overridden_by`, `override_reason`, recompute `net_usdt = gross_usdt - new_commission (- gas_usdt)`; reject if it would raise commission.
- Handlers: admin sees a `pending_admin` list; per payout: «Одобрить» (release), «Отклонить» (reject), optional «Занизить комсу» → prompt amount → `reduce_payout_commission` then release. Every action audited with reason.

- [ ] **Step 1** Failing tests: release flips to `reserved`; reject → `cancelled` + accruals `available`; `reduce_payout_commission` only-down (raising rejected), recomputes net upward. (Real Postgres, seed a `pending_admin` payout + reserved accruals.)
- [ ] **Step 2** Run — expect fail.
- [ ] **Step 3** Implement repo + service + handlers.
- [ ] **Step 4** Run — expect pass. Gate + commit (`feat(admin): release/reject pending_admin payouts + one-time commission reduction`).

---

## Task 5: Personal rate (B), UID disputes, user block/unblock

**Files:** Extend `bot/admin/{repository,service,router,keyboards}.py`; Test `tests/admin/test_controls.py`.

**Interfaces / behavior:**
- **Personal rate:** admin handler → prompt user_id + pct → validate `0 <= pct <= 1` → `set_commission_override(user_id, pct, since=today, by=admin)` + audit; «Сбросить» → `clear_commission_override`. (The worker already honors it, Task 2.)
- **UID dispute:** `repo.reassign_uid(session, exchange, uid, new_user_id, by)` — archive the current active `(exchange, uid)` account and create an active one for `new_user_id` (same uid); or `repo.block_uid(session, exchange, uid, by)` — set the account `status='blocked'`. Only future accruals affected. Audit.
- **Block/unblock:** `set_user_status(user_id, "blocked" | "active")` + audit.

- [ ] **Step 1** Failing tests: set/clear override persists on `users`; pct out of range rejected; `reassign_uid` moves ownership (old archived, new active); `block_uid` blocks; `set_user_status` toggles. (Real Postgres.)
- [ ] **Step 2** Run — expect fail.
- [ ] **Step 3** Implement.
- [ ] **Step 4** Run — expect pass. Gate + commit (`feat(admin): personal commission rate, UID reassign/block, user block/unblock`).

---

## Task 6: Kill-switch + blocked-user enforcement (executor + withdraw)

**Files:** Modify `bot/executor/executor.py`, `bot/botui/withdraw.py`, `bot/botui/repository.py` (a `get_user_status` helper if needed); Test `tests/executor/test_killswitch.py`, `tests/botui/test_withdraw_blocked.py`.

**Behavior:**
- `executor.process_next`: at the very top, if `admin_repo.is_payouts_paused(session)` → return (send nothing). (Read the flag inside the first session.)
- `botui.withdraw.prepare_withdrawal`: if `is_payouts_paused` → `WithdrawBlocked("paused")`; if the user's `status == 'blocked'` → `WithdrawBlocked("blocked")`. (Add reasons + Russian messages in the handler.)

- [ ] **Step 1** Failing tests: with `payouts_paused=true`, `process_next` does NOT send a ready reserved payout (stays `reserved`, `chain.sent_calls == []`); `prepare_withdrawal` returns `WithdrawBlocked("paused")`. Blocked user → `WithdrawBlocked("blocked")`. Un-pause → normal flow resumes.
- [ ] **Step 2** Run — expect fail.
- [ ] **Step 3** Implement the guards.
- [ ] **Step 4** Run — expect pass. Gate + commit (`feat(admin): kill-switch + blocked-user guards in executor and withdraw`).

---

## Task 7: Wiring, admin bootstrap, quality gate

**Files:** Modify `bot/main.py`; `bot/botui/repository.py` (upsert_user grants admin from env); Test `tests/admin/test_bootstrap.py`; update `autonomous_todo.md`, ledger.

**Behavior:**
- `upsert_user(session, user_id)` — when inserting (and on existing rows too, idempotently), if `user_id in settings.admin_telegram_ids`, set `role='admin'`. (Pass the admin set in, or read settings; keep it testable — prefer a param `admin_ids: frozenset[int] = frozenset()` defaulted, and the caller/menu passes `settings.admin_telegram_ids`.) Simplest: a dedicated `ensure_role(session, user_id, admin_ids)` called from the menu `/start` and `/admin`.
- `build_dispatcher`: `dp.include_router(admin_router)` (before `errors`); pass `settings.admin_telegram_ids` where needed (e.g. `dp["admin_ids"]`).
- Full gate + docs.

- [ ] **Step 1** Failing test: a user whose id is in `ADMIN_TELEGRAM_IDS` gets `role='admin'` on ensure; others stay `user`.
- [ ] **Step 2** Implement bootstrap + wiring.
- [ ] **Step 3** Full gate: `ruff`/`format`/`mypy bot` clean; `./.venv/Scripts/pytest.exe -q` green. Update `autonomous_todo.md` (Phase 6 done; note deploy-time: Postgres RLS, real risk-flags/antifraud + UID-ownership verification still deferred; first admin via env) + ledger.
- [ ] **Step 4** Commit (`feat(admin): register admin router + env admin bootstrap`).

---

## Documented assumptions (owner: review)
1. **Commission override (B)** = `min(rank_fee, override)` at freeze, from `since` forward; past frozen rows unchanged; only-down. Absolute personal rate, capped at rank.
2. **First admin** via `ADMIN_TELEGRAM_IDS` env (can't be granted in-bot). Default includes nobody unless set; consider seeding `DEVELOPER_TELEGRAM_ID`.
3. **Postgres RLS / DB-level roles** deferred to deploy (app-level `IsAdmin` gate in this phase).
4. **Antifraud / UID-ownership verification / auto risk-flags** remain deferred (§2.2) — admin dispute resolution is manual; reassigning a UID affects only future accruals (already-paid funds are on-chain, irreversible).
5. **One-time payout commission reduction (A)** included at release (fields exist) in addition to the persistent rate (B).

## Self-Review (author)
- Coverage: override (B) money-path (Tasks 1-2), role gate (3), release/reject+one-time-reduction (4), personal rate + UID dispute + block (5), kill-switch/blocked enforcement across executor+withdraw (6), bootstrap+wiring (7). Deferred items (RLS, antifraud) explicitly listed.
- Money-critical worker change (Task 2) is isolated, only-down, `since`-gated, freeze-preserving, and regression-tested; complete code given for Tasks 1-2.
- Names consistent: `effective_fee`, `get_commission_override`, `IsAdmin`, admin repo transitions; reuse `write_audit`, `expire_aged`, executor/withdraw entry points.
