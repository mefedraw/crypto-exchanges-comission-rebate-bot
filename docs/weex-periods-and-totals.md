# WEEX Periods and Owner Totals

WEEX commission queries use exact timezone-aware instants, converted to integer
milliseconds. A UTC+8 calendar month therefore starts at 16:00 UTC on the previous
UTC date. Inclusive API windows are split without overlapping milliseconds.

The accrual worker stores exact `query_from`/`query_to` for new WEEX rows. For a
legacy finalized row without those fields, its original UTC-calendar coverage is
preserved. The first new UTC+8 day starts after the old coverage ends; already
counted hours are not queried again. Gate keeps its existing second-resolution
cutover behavior, while WEEX uses milliseconds. No schema migration is needed.

Existing paid/expired/finalized history is not silently moved, repriced or
re-credited by this adapter change. Historical date repairs require an individually
verified, audited plan because one old UTC day can contain parts of two UTC+8
business days. Shifting all old dates by one day is not valid.

For the whole-channel admin summary, `getAffiliateUIDs` must not use the income
period: that endpoint's registration-date filter omits old UIDs with new income.
UIDs are instead discovered from every page of unfiltered SPOT and FUTURES
commissions over the exact requested period. Discovery amounts are never summed:
live responses can include parent income AND subsidiary income for the same trade.
Each discovered UID is queried through the existing owner-scoped commission API;
only its USDT commission is added to the owner's summary.

Duplicate discovery UIDs are deduplicated. Invalid UIDs, malformed pages, changing
record totals and incomplete pagination fail the summary rather than becoming a
zero or a partial total. Zero is returned only for a complete empty response.
Request pacing leaves per-process headroom in the shared IP-weight budget.

Tests cover exact UTC+8 bounds, a 31-day month, clipped legacy coverage, repeated
worker cycles, short intermediate pages, subsidiary-income exclusion and API
failure propagation. Historical private financial exports and real customer IDs
must not be included in public fixtures.
