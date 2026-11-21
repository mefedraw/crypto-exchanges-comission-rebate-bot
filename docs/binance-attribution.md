# Binance Referral Pro: reconciliation before enabling accruals

Binance Link (Api-Agent) customer IDs do not establish Referral Pro invitee
ownership. The Spot taxQuery response has no invitee UID. Never assign these
account-level amounts to a bot user, even if only one UID is linked.

Keep the UID hold in place until a read-only partner source or a verified
Referral Pro report is available. Links, bindings, and historical ledger rows
are not removed by the hold.

Before posting any corrections:

1. Confirm the referrer's account and program, report timezone, coverage,
   settlement lag, archive limits, and documented meaning of the amounts.
   Require the invitee UID, amount and asset, transaction date, reward type,
   status and a stable identity for deduplication; reject partial pages.
2. Match each UID to its eligible binding and dates. Exclude the invitee's own
   kickbacks, failed rewards, duplicates and unrelated accounts. Group by day
   and asset with Decimal. USD display values, USDC, BNB and USDT are not
   interchangeable. Do not convert assets without an approved valuation policy.
3. Reconcile a closed period against the partner dashboard for each UID.
   Dry-run a per-user difference against the actual ledger (all states and past
   corrections) using the applicable historical rank, fee and override. Review
   effects on other exchanges' ranks, partner rewards, and expiry of old claims.
4. Review the preview before posting uniquely keyed corrections. Reconcile
   overlapping exports and normal worker coverage; a second identical run must
   add nothing. Never edit paid rows or trigger a transfer as part of import.
5. Only resume automatic accrual after pagination, delayed settlement,
   UTC+8 boundaries, mixed-user failures and retry idempotency are tested
   against the verified source.
