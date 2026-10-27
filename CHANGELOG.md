# Changelog

All notable changes to this project will be documented in this file.

The format loosely follows Keep a Changelog, and this project uses calendar dates
for unreleased work snapshots.

## [Unreleased]

### Repository Synchronization

- Restored the deployed application's source, PostgreSQL migrations, tests,
  translations and operational tooling from a source-only production snapshot.
- Preserved all runtime source files; excluded secrets, customer exports,
  private financial audits and host-specific deployment overrides.
- Added missing test dependency metadata, refreshed the lockfile, and fixed
  three date-sensitive tests without changing application behavior.

### Added

- Added persistent user language settings and seven additional UI languages:
  English, Ukrainian, Simplified Chinese, Turkish, Hindi, Thai, and Vietnamese.
- Localized cabinet screens, support statuses, partner views and background
  notifications with offline gettext catalogs; retained Russian for existing
  users and the admin UI, and preserved first-registration invite attribution.
- Added whalesincryptoio custom phone and gift icons to the support and invite
  main-menu buttons, preserving labels, callback actions, and the MNT layout.
- Connected referral buttons and detail headings to the whalesincryptoio custom
  emoji pack, with verified exchange logos and a neutral pack icon for others.
- Added Telegram emoji-permission fallback without changing links or callbacks,
  serialization coverage for pinned aiogram, and a successful owner-only API probe.
- Added four durable support statuses, explicit admin take-in-work action,
  owner-reply reopening, active/closed admin lists, and migration a258b106e347.
- Renamed the admin support entry to "Обращения" and added status labels to cases.
- Added short owner notifications for reply-required and closed cases, with an
  open-case button, stale-request suppression, and legacy-outbox compatibility.
- Completed the referral-link catalog with BitBase, Bitunix, Lbank, Blofin,
  and QFEX; restored per-exchange emoji and explicit pending statuses.
- Matched referral URLs and displayed terms to the owner's list, retained
  BitMart, and removed the unsupported percentage previously shown for RiseX.
- Limited new support cases to two per rolling 24 hours across all UIDs,
  including closed cases, with an early UI check and locked final enforcement.
- Added persistent monthly partner ranks, active-referee lifetime checks,
  three-calendar-month promotion protection, and idempotent one-step reviews.
- Added incomplete-data holds, private rank history and change notifications,
  concurrency/financial regression coverage, and migration f147a095d236.
- Clarified that 30% activity is informational: a closed step is retained,
  while an unclosed step can decline after protection regardless of this ratio.
- Added direct-invitation revenue share with seven independent partner ranks,
  alternative qualification criteria, lifetime finalized gross checks, and
  immediate funded allocations from new owner fees only.
- Added separate partner balances, atomic reservations, owner-bound single-use
  confirmation tokens, and USDT/MNT payouts through the existing executor.
- Added source-fee funding protection, partner-aware settlement/release, private
  rank progress, owner profit after allocations, and migration e0369f84c125.
- Added personal bot invitation links with automatically issued five-character
  alphanumeric codes, preserved old links, and private invitation counters.
- Added atomic first-registration attribution, anti-self-referral and reparenting
  protections, concurrent name-claim handling, and migration d9258e73b014.
- Added persistent support cases: owned exchange/UID selection, text preview,
  explicit submission, conversation history, staff replies, and case closure.
- Case creation requires 100 USDT of lifetime finalized gross rebate, including
  already-paid amounts; the entry button remains visible below the threshold.
- Added transactional support notifications, idempotent submissions, ownership
  checks, anti-spam limits, and migration c8147d62a903.
- Added separately audited, idempotent Gate reconciliation credits and an explicit
  operator entry point; existing accrual amounts and paid rows are retained.
- Added migration b702a3647f18 and correction tests for retries, concurrent
  requests, rollback, regular accrual upserts, and payout reservation.
- Added an admin-only `/api_keys` flow for live validation and rotation of
  affiliate/read API credentials.
- Added an isolated root updater with Unix peer authentication, a strict
  exchange-field and service allowlist, atomic `0600` writes, and rollback.
- Added BitMart Futures affiliate rebate support via the KEYED futures API.
- Added `BITMART_API_KEY` configuration; `BITMART_API_SECRET` is optional for the
  current BitMart futures affiliate endpoints.
- Added tests for BitMart multi-currency rebate parsing and KEYED request headers.

### Changed

- Grouped user support under "Техническая поддержка", with "Создать новое
  обращение" and "Мои обращения"; only creation retains the 100 USDT gate.
- Result rendering now preserves all returned assets instead of showing only
  USDT, so BTC/ETH rebate lines are not silently dropped.
- Updated user-facing help text and project documentation to describe
  multi-currency results.

### Fixed

- Withdrawal confirmations now bind tokens to the actor and operation and
  serialize rebate/partner confirmations on the same user lock.
- Partner transfers preserve sub-cent amounts at 18 decimals without changing
  existing rebate rounding; late failures cannot resurrect paid allocations.
- Removed custom invitation-code entry and disabled existing custom-code buttons;
  legacy links remain valid when their owner receives a shorter code.
- Historical Gate credits participate in balances and payouts without moving
  the API collection cursor or being repriced by a later fee override.
- Gate queries preserve exact timezone-aware bounds, including UTC+8 payout days,
  and paginate over non-overlapping second-based windows.
- Gate accrual waits one additional closed day for late commission batches.
- Exact Gate query coverage is persisted; legacy UTC rows retain their original
  coverage so the UTC+8 cutover cannot double-count the overlapping eight hours.
- Migration 93ecac0880f5 adds nullable query_from/query_to metadata without
  rewriting historical monetary amounts or paid rows.
- Credential cancellation now deletes the prompt and keyboard without posting
  a cancellation notice or a replacement menu.
- Fixed credential cancellation callbacks being silently rejected when aiogram
  decoded the empty exchange code as `None`; existing cancel buttons still work.
- Added dispatcher-level coverage for cancellation from each credential input step.

### Notes

- BitMart `rebate-user` returns rebate data without a currency field, so the bot
  uses the futures `rebate-list` endpoint to keep the BTC/USDT/ETH split.
