# Binance Referral Pro

The bot credits **one gross USDT per USD of dated invitee commission**. This
is an explicit owner-funded accounting policy, not a market quote or evidence
that the exchange transferred USDT. No BNB, USDC or other wallet entries are
added on top. See [Binance's explanation of display valuation and settlement](https://www.binance.com/en-BH/support/faq/detail/63572e23acda4bc891fd605e93335669).

Use the signed read-only `GET /sapi/v1/affiliate/performance/invitee` with
`userId`, inclusive `startDate` and `endDate` (YYYY-MM-DD). A successful
profile has registration time and referral code; reject conflicting UID,
malformed payloads, negative or nonfinite commission. Only HTTP 400/-29002
means the UID does not belong to this affiliate. A rejection or a profile
without lifetime activity is rechecked later; neither creates zero rows or
blocks siblings. Never attribute Binance Link, wallet dividends, referral-code
totals or a whole-downline rollup to any individual.

The worker uses closed calendar labels and a two-day settlement buffer (or a
longer lag from a sibling exchange). Missing commission is not zero: absent
metrics need proof from registration or matching dated gross **and volume**
with and without the missing interval. Ambiguous data remains unresolved.
Historical ranks, next-day upgrades and ordinary fee overrides apply.
From the 2026-11-23 earning day (UTC+8), add one percentage point to the
effective fee after any personal discount. For 100 USD at a 10% base fee the
booked gross is 100 USDT, fee 11, net 89. Late or recovered earlier days still
use their historical rate. The admin total includes the gross of linked UIDs,
not the fee again and not the whole affiliate account.

## Reviewed historical cutover

1. Run `python -m bot.accrual.binance_recovery --through YYYY-MM-DD` from a
   worker environment. It reads all active Binance bindings, compares daily
   gross against separately fetched range reports and calculates the fees
   including sibling accruals. Production dry-run uses read-only transactions.
2. Stop the accrual worker for the cutover. Review per-UID gross, new net, and
   rejected UIDs. Run the same command with `--apply-digest <reviewed digest>`.
   Changed source, mismatched prior credits, unresolved sibling fee changes
   or historical referral-share ambiguity block posting. No network calls
   while holding ledger locks. No wallet transfer takes place.
3. Run a fresh read-only preview: new rows and net must both be zero.
   Inspect the audit record and available balance, then resume the worker.
   Original earning dates are retained; only newly recovered rows receive a
   fresh 30-day claim window. Never re-open a paid row or reset its expiry.
