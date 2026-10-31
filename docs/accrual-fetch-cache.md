# Durable Commission Fetches

Successful per-account API answers are committed independently of the user's
financial transaction. Failed requests never create a zero-valued snapshot.
The worker collects other accounts even when an earlier account fails. After
one API failure for an account, its remaining dates wait for the next cycle
to avoid hammering an unavailable exchange.

The financial calculation starts only once ALL required account windows for
the user have been collected. Rank, fee, expiry, settlement lag, partner revenue
and paid/frozen accrual rules are unchanged. This feature reduces duplicate API
requests; it does NOT make other balances advance during a prolonged MEXC block.
That requires a separate, explicitly designed partial-settlement policy.

## Storage

`commission_fetches` is not a ledger and is never read by balance/withdrawal
queries. Identity is account + accrual day + exact query bounds + format version.
This keeps Gate's clipped legacy-cutover window distinct from a full day.

Payloads contain only the normalized CommissionResult: separate assets and
sources, decimal strings, record count and adapter notes. No headers, credentials,
full raw API bodies or FX quotes are stored. MNT valuation still uses the current
cycle's successful rate; storing a native MNT amount does not freeze its rate.

Snapshots survive restarts and financial rollbacks. Concurrent successful writes
use the same first committed fresh winner. Invalid payloads fail
closed instead of silently producing zero. Genuine successful empty/zero answers
are cached as successes, subject to the existing settlement-lag rule.

## Release

Migration `c47ad328e569` adds only the snapshot table after `b369c217f458`.
It does not rewrite historical ledger, payout or user rows. Deploy the migration
before the updated worker. The previous worker remains schema-compatible for
application rollback; dropping snapshots is unnecessary for rollback.

Successful answers are reused for up to 24 hours, then revalidated so a prolonged
outage cannot settle against indefinitely old responses. A failed refresh keeps
the previous snapshot for inspection but DOES NOT use it as a financial fallback.
Only a new successful response updates its timestamp/payload. If adapter
interpretation changes, bump `bot.accrual.fetches.FORMAT_VERSION`, or deliberately
invalidate affected snapshot windows before retrying. Already-finalized ledger
entries are still not re-polled or changed. Correcting those requires the existing
separate reconciliation workflow. Credential ownership changes also require
review of pending snapshots, not blind reuse of previous-account data.
