# Partner Exchange Integrations

These integrations use affiliate/read credentials, not trading or withdrawal
permissions. Credentials may be Fernet-encrypted with the existing MASTER_KEY.
Both `*_API_SECRET` and the owner's `*_SECRET_KEY` spelling are accepted;
`*_API_SECRET` takes precedence. No database migration is required.

## BitBase

Source: supplied `Partner Commission User Data Query OpenAPI-Single UID.pdf`,
11 pages, production Partner OpenAPI.

- Host: `https://www.bitbase.com` (never the QA host shown in a curl example).
- UID amount: `GET /openapi/partner/commission-user`, `userId`, `role=ALL`.
- Whole-channel total: `GET /openapi/partner/commission-users`, checked pagination.
- Sum `spotCommissionAmountU` and `futuresCommissionAmountU` once, verify their
  sum equals `commissionAmountU`. Fees, volume, deposits and balance are not income.
- Amounts are USDT. Dates are inclusive UTC calendar days, at most 31 per window.
  They cannot express an arbitrary UTC+8 intraday interval. Worker date labels
  map to the same named UTC calendar day, explicitly disclosed in adapter notes.
- List queries cannot include UTC today; unavailable totals stay unavailable.
- Auth: sorted query and the five `validate-*` headers, HMAC-SHA256 hex.
- `COSMOS_1012` is ambiguous (permissions or outside hierarchy), not a definite
  membership rejection. Commission errors never become a successful zero.

## Bitunix

Source: partner-supplied [Partner API specification](https://docs.google.com/document/d/1uNKbWReCeCjAq82I-yjd4-9wGejM-Zxxguwc1O_AUTY/mobilebasic).

- Host: `https://partners.bitunix.com`, separate from the futures trading API.
- UID amount: `GET /partner/api/v1/openapi/totalTransaction`, `userType=agent`.
- Preserve `currency` and `tradeType`; sum only `totalCommission`. Do not sum
  native assets together or convert them at enquiry-time FX rates for payouts.
- Whole-channel dashboard: `GET /partner/api/v1/openapi/totalCommission` (the
  exchange converts this aggregate into USDT; not a per-user payout source).
- Referral check: signed form POST `/partner/api/v2/openapi/validateUser`; this
  checks direct invitees only, so false is inconclusive for a downstream user.
- Absolute UTC second bounds retain the caller's timezone, without overlapping
  windows. A conservative 180-day window is used for totalTransaction.
- Auth: partner protocol SHA1 of ordered values plus secret; timestamp seconds
  expires after 30 seconds. Follow the document's executable ASCII-sum ordering.

## BloFin

Source: [official Affiliate API](https://docs.blofin.com/index.html#affiliate).

- Host: `https://openapi.blofin.com`.
- UID amount: `GET /api/v1/affiliate/invitees/daily/info`, direct invitees only.
- Sum `commission`, never `fee`, `cashback` or trading volume. Require matching
  UID and timestamps, strict data shape, and progress during time-cursor paging.
- The schema omits currency. `BLOFIN_COMMISSION_ASSET` must explicitly name the
  denomination confirmed in the affiliate portal. Missing currency disables the
  adapter rather than assuming USDT. Owner confirmed USDT for this deployment.
- Preserve exact caller bounds at millisecond precision; daily records cannot
  be prorated into individual trades. Do not substitute cumulative totals.
- Requires key, secret and passphrase. Auth is base64 of the ASCII hex HMAC,
  timestamp milliseconds and a fresh nonce, regenerated on retries.
- No whole-channel period total is implemented: render unavailable, not zero.

## Operational Guardrails

All three adapters use a one-closed-day settlement buffer and conservative local
pacing. This is an operational precaution, not an exchange guarantee of finality.
Malformed responses and business errors raise; only explicit successful empty
responses produce zero. HTTP failures never echo response bodies or credentials.

`DISABLED_EXCHANGES` continues to gate API polling and new UID bindings. Secrets
are passed only to bot/accrual roles, never the payout executor. The credential
updater allows only each exchange's encrypted key/secret/passphrase fields; the
non-secret BloFin denomination is configured separately by the operator.
