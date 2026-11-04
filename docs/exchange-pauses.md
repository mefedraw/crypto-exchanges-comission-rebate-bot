# Administrative exchange pauses

`DISABLED_EXCHANGES=mexc,bitget` pauses an integration independently of whether
its credentials exist. It is read by both the bot and accrual worker. Unknown
codes reject configuration rather than silently leaving an exchange active.

- No automatic adapter construction, commission requests or downline polling.
- No new UID binding, including stale buttons and already-open input sessions.
- Existing UID ownership, account statuses, cursors, encrypted credentials,
  historical accruals, partner allocations and payout records remain intact.
- Paused UID entries remain visible with a pending label. Referral links remain
  pending; the administrator may still rotate credentials to repair the account.
- Enabled sibling accounts continue accruing. Previously finalized amounts on
  paused accounts still contribute to rank calculations. Missing paused data is
  not stored as zero and does not advance the account's finalized cursor.
- A user with only paused accounts generates no missing-adapter failure.

BitMart is retired, not merely paused: its adapter, credential fields/rotation and
referral entry are removed. Legacy database records, if any, are retained. The
worker recognizes that retired code so old records cannot break other accounts.

To resume MEXC or Bitget, repair its upstream permissions, remove its code from
`DISABLED_EXCHANGES` in both role configurations and recreate bot/accrual services.
The worker can fetch pending history from its unchanged cursor. Already-finalized
money is never rewritten. If delayed data would have changed fees on previously
finalized sibling rows, reconciliation requires separately reviewed idempotent
corrections; automatic historical fee repricing is not part of this change.

This administrative pause is not general financial isolation for an unexpected
failure on an enabled exchange. Enabled exchanges retain the atomic per-user
financial transaction. The separate accrual-fetch-cache PR saves successful API
responses independently but does not change that financial transaction boundary.
