# Binance Referral Pro: reconciliation before enabling accruals

Binance Link (Api-Agent) customer IDs do not establish Referral Pro invitee
ownership. The Spot taxQuery response has no invitee UID. Never assign these
account-level amounts to a bot user, even if only one UID is linked.

The signed read-only `GET /sapi/v1/affiliate/performance/invitee` takes
`userId`, and optionally inclusive `startDate` and `endDate` as YYYY-MM-DD.
Verified from the worker IP against linked accounts on 2026-11-21: a member
returns HTTP 200, code `000000000`, and an invitee profile; for two other
bindings Binance returned HTTP 400, code `-29002` (not this affiliate's
invitee). Do not use a timeout, parse error, or authentication failure as a
negative membership decision. The dashboard USD amounts matched both the
period and lifetime readings to the precision displayed. The API has no asset,
distribution status, or transaction identity. A missing amount is not zero.

The [Affiliate Performance SDK implementation](https://github.com/JKorf/Binance.Net/pull/1598)
was contributed using documentation from Binance Business Development. Its
other method is grouped by referral code, not by payout transaction.
[Binance describes the default USDC settlement and the separate USD display valuation](https://www.binance.com/en-BH/support/faq/detail/63572e23acda4bc891fd605e93335669).
Neither the default currency nor an original BNB trading fee makes the USD
display amount the native amount received. Do not join independent reports by
timestamp or approximate value. The [Spot wallet dividend endpoint](https://developers.binance.com/docs/wallet/asset/assets-divided-record)
does return an amount and asset, but its entries do not identify the invitee.
The [USD-M income history](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Get-Income-History)
also lacks an invitee UID: on our read-only check the `REFERRAL_KICKBACK`
records contained a masked address in `info`. A masked address, trade ID or
wallet transaction ID is not proof of ownership of an invitee UID.

Keep the payout hold until the API or the official **My Commission > Commission
Rebate** report identifies actual distributions by invitee and asset. Do not
import **Shared Commissions** or a Users/Performance export as a payout report.
Binance's [referral settlement announcement](https://www.binance.com/en/support/announcement/detail/9490ef81489844a6babc385eba238d75)
describes where to find the original token, quantity and actual converted USDC
for the covered Spot and Margin products. Do not apply that settlement rule to
Futures without matching distribution records.
No exchange keys, login cookies or HAR files should be shared. Links, bindings
and historical ledger rows are not removed by the hold.

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
