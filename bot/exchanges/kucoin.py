"""KuCoin affiliate commission adapter.

Endpoint: GET /api/v2/affiliate/queryTransactionByUid
Docs: https://www.kucoin.com/docs-new/rest/affiliate/get-trade-history

Per-invitee commission over an arbitrary date range: sum each trade's `commission`
by `currency`.

WHY NOT queryMyCommission: that endpoint is the affiliate's AGGREGATE commission
(daily buckets with no invitee field), and its `userId` parameter is SILENTLY
IGNORED — live-verified: three different uids (incl. bogus ones) returned the exact
same total. Using it meant every referral was credited the WHOLE affiliate rebate.
queryTransactionByUid takes a REQUIRED `uid` and genuinely filters per invitee
(live-verified: distinct results per uid; bogus uid -> 0 records).

WINDOW IS PLAIN UTC (no UTC+8 shift): live-verified that passing a UTC+8-shifted
window to this endpoint SILENTLY RETURNS ZERO trades for a uid that has them
(deterministic: a July window returned 0 shifted vs 32 un-shifted), whereas the
un-shifted UTC window returns the correct rows. So a KuCoin accrual day is a UTC
day; period totals are unaffected (windows tile contiguously) — only a boundary
trade's day attribution can differ by <8h, immaterial to payouts.

CODE 300000 "user not found" == 0 for that uid: live-verified this is DETERMINISTIC
per uid, not flaky — a uid that isn't a resolvable KuCoin account returns HTTP 400
code 300000 on every call, while a real invitee returns rows (or an empty list for a
window with no trades, e.g. before they registered). Real invitees are gated by
verify_referral (queryInvitees) before they're ever queried here, so they don't hit
300000; a mistyped/non-KuCoin uid that somehow does legitimately earns nothing. So we
treat 300000 as an empty window rather than crash the accrual cycle. (This is safe
BECAUSE it's deterministic-per-uid: it can never silently zero a real invitee's
commission — they never return 300000.)
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ReferralCheck
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_base64
from bot.utils.dates import iter_windows, to_millis
from bot.utils.money import parse_decimal

# VERIFIED (live + docs): per-invitee trade history; REQUIRED `uid` filter,
# `tradeStartAt`/`tradeEndAt` window, each item carries `commission` + `currency`.
_PATH = "/api/v2/affiliate/queryTransactionByUid"
# VERIFIED (official KuCoin "Get Invited" docs): the affiliate's invitee list,
# filterable by uid, returned regardless of rebate amount — the membership check.
_INVITEES_PATH = "/api/v2/affiliate/queryInvitees"
# VERIFIED (official KuCoin "Get Commission" docs): the affiliate's AGGREGATE
# commission — the endpoint get_commission deliberately avoids for per-uid queries
# (see module docstring: `userId` is documented as optional and live-verified to be
# SILENTLY IGNORED, so every call returns the WHOLE downline's rebate). That
# whole-rebate behavior is exactly what get_downline_total wants. Response is a
# bare list under `data` (not `data.items`), each item carrying `commission` +
# `currency` (no per-uid field) — confirmed via current KuCoin docs.
_MY_COMMISSION_PATH = "/api/v2/affiliate/queryMyCommission"
_PAGE_SIZE = 100  # VERIFIED (live): page/pageSize pagination, items under data.items.
_USER_NOT_FOUND_CODE = "300000"  # "user not found" — uid not a KuCoin account => 0
# VERIFIED (live): queryTransactionByUid SILENTLY returns 0 rows for a narrow window
# (<~week) even when trades exist in it — a 1-day accrual query missed real commission
# entirely. So we FETCH a WIDE window (the whole calendar month spanned, padded) and
# keep only the trades whose ``tradeTime`` falls inside the requested [date_from,
# date_to]. Attribution is by real trade time (timezone-independent, no double-count).
#
# The wide fetch is CACHED per (uid, month-window) for a short TTL so the accrual
# worker's per-day calls within a month reuse ONE fetch instead of re-fetching a wide
# window every single day — without the cache, a user with several KuCoin accounts (or
# a catch-up over many days) hammered the endpoint into HTTP 429, failing the whole
# user's accrual cycle. The month anchor keeps the requested day away from the window
# edges (KuCoin needs a wide window to return rows at all).
_FETCH_PAD_DAYS = 2  # extend the month window slightly so boundary days aren't at the edge.
_CACHE_TTL_S = 120.0  # long enough to cover one user's day-loop; short enough to stay fresh.


def _fetch_bounds(d0: date, d1: date) -> tuple[date, date]:
    """A wide, STABLE fetch window covering the calendar month(s) that [d0, d1] spans,
    padded by ``_FETCH_PAD_DAYS``. Stable so every per-day call within a month maps to
    the same window (one cached fetch)."""
    first = d0.replace(day=1)
    after = date(d1.year + 1, 1, 1) if d1.month == 12 else date(d1.year, d1.month + 1, 1)
    return first - timedelta(days=_FETCH_PAD_DAYS), after - timedelta(days=1 - _FETCH_PAD_DAYS)


class KucoinAdapter(BaseHttpAdapter):
    name = "KuCoin"
    code = "kucoin"
    base_url = "https://api.kucoin.com"  # VERIFIED: KuCoin REST host.
    # VERIFIED: affiliate history has a high request weight; pace pagination and
    # concurrent user lookups so one UID cannot immediately exhaust the pool.
    min_request_interval_seconds = 1.0
    # Per-record payout commissions are additive across windows (live-verified);
    # 30-day chunks are a safe default (KuCoin accepted them).
    max_window_days = 30
    supports_uid_filter = True
    supports_date_range = True

    def __init__(self, credentials: ExchangeCredentials, settings: Settings) -> None:
        super().__init__(credentials, settings)
        # KuCoin settles partner commission with a delay — freezing a day too early would
        # capture commission still at 0. The worker defers the freeze by this many days.
        self.settlement_lag_days = settings.kucoin_settlement_lag_days
        # Short-lived cache of raw trades per (uid, fetch-window) — see the module
        # constants: it collapses the worker's per-day fetches to one per uid/month.
        self._trade_cache: dict[tuple[str, int, int], tuple[float, list[dict[str, object]]]] = {}

    def _headers(self, method: str, endpoint: str) -> dict[str, str]:
        """KuCoin v2 signing (HMAC-SHA256 base64; passphrase is also signed)."""
        # VERIFIED: SIGN = base64(hmac_sha256(timestamp + method + endpoint + body)).
        timestamp = str(int(time.time() * 1000))
        secret = self._creds.api_secret.get_secret_value()
        passphrase = self._creds.passphrase.get_secret_value() if self._creds.passphrase else ""
        sign = hmac_base64(secret, f"{timestamp}{method}{endpoint}")
        signed_passphrase = hmac_base64(secret, passphrase)
        return {
            "KC-API-KEY": self._creds.api_key.get_secret_value(),
            "KC-API-SIGN": sign,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": signed_passphrase,
            "KC-API-KEY-VERSION": "2",
            "Accept": "application/json",
        }

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        result = self._new_result(uid, date_from, date_to)
        self._log.info("get_commission", uid=uid, date_from=str(date_from), date_to=str(date_to))

        # Fetch a WIDE month window ONCE (cached), then keep only trades whose
        # tradeTime is inside the REQUESTED range. Attribution is by real trade time,
        # so a per-day accrual query gets exactly that day's trades regardless of the
        # request's timezone — and the cache stops per-day re-fetching (429 guard).
        lo_ms, hi_ms = to_millis(date_from), to_millis(date_to)
        for item in await self._trades_for(uid, date_from.date(), date_to.date()):
            raw_tt = item.get("tradeTime")
            try:
                tt_ms = int(str(raw_tt))
            except (TypeError, ValueError):
                # No usable trade time: cannot attribute to a day, so skip (never count
                # it for every overlapping day-window). Real records always carry it.
                continue
            if not (lo_ms <= tt_ms <= hi_ms):
                continue
            # VERIFIED (live): each trade exposes `commission` + `currency`.
            result.add_amount(str(item["currency"]), parse_decimal(str(item["commission"])))
            result.raw_records_count += 1

        result.settlement_note = "Сумма комиссий KuCoin за период (по данным API)."
        return result.finalize()

    async def _trades_for(self, uid: str, d0: date, d1: date) -> list[dict[str, object]]:
        """All raw trades for ``uid`` over the wide month window covering [d0, d1],
        served from a short-TTL cache so repeated per-day calls hit the API once."""
        fetch_from, fetch_to = _fetch_bounds(d0, d1)
        key = (uid, fetch_from.toordinal(), fetch_to.toordinal())
        now = time.monotonic()
        cached = self._trade_cache.get(key)
        if cached is not None and now - cached[0] < _CACHE_TTL_S:
            return cached[1]
        trades = await self._fetch_trades(uid, fetch_from, fetch_to)
        # Drop stale entries so the cache can't grow without bound, then store.
        self._trade_cache = {
            k: v for k, v in self._trade_cache.items() if now - v[0] < _CACHE_TTL_S
        }
        self._trade_cache[key] = (now, trades)
        return trades

    async def _fetch_trades(
        self, uid: str, fetch_from: date, fetch_to: date
    ) -> list[dict[str, object]]:
        """Page through every <=30-day chunk of [fetch_from, fetch_to] and return all
        raw trade rows. A 300000 "user not found" (deterministic per uid) means the uid
        earns nothing -> empty."""
        trades: list[dict[str, object]] = []
        for window_start, window_end in iter_windows(fetch_from, fetch_to, self.max_window_days):
            rows = await self._fetch_window(uid, window_start, window_end)
            if rows is None:  # user-not-found -> the whole uid is empty
                return []
            trades.extend(rows)
        return trades

    async def _fetch_window(
        self, uid: str, window_start: datetime, window_end: datetime
    ) -> list[dict[str, object]] | None:
        """One <=30-day window, paginated. Returns the raw rows, or None if KuCoin
        reports 300000 (uid not a resolvable account)."""
        rows: list[dict[str, object]] = []
        page = 1
        while True:
            params = {
                "uid": uid,  # REQUIRED filter — the whole point (queryMyCommission ignored it)
                # Plain UTC window: a UTC+8 shift makes this endpoint drop the rows (see
                # module docstring). tradeStartAt/tradeEndAt are absolute trade times.
                "tradeStartAt": to_millis(window_start),
                "tradeEndAt": to_millis(window_end),
                "page": page,
                "pageSize": _PAGE_SIZE,
            }
            endpoint = f"{_PATH}?{urlencode(params)}"
            try:
                payload = await self._send(
                    "GET", endpoint, headers=self._headers("GET", endpoint), log_path=_PATH
                )
            except ExchangeApiError as exc:
                if _USER_NOT_FOUND_CODE in str(exc):
                    self._log.warning("kucoin user-not-found -> treating as empty", uid=uid)
                    return None
                raise
            self._check_envelope(payload)
            items = self._extract_items(payload)
            rows.extend(items)
            if len(items) < _PAGE_SIZE:
                break
            page += 1
        return rows

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        """Whole-downline USDT rebate via ``queryMyCommission``.

        Unlike :meth:`get_commission` — which is careful to use
        ``queryTransactionByUid`` because ``queryMyCommission``'s ``userId`` filter is
        silently ignored (see module docstring) — this method calls
        ``queryMyCommission`` ON PURPOSE and with NO uid filter at all: its
        whole-affiliate aggregate is exactly what a downline total wants. Only the
        USDT currency bucket is summed, per the base-class contract.
        """
        total = Decimal(0)
        for window_start, window_end in iter_windows(
            date_from.date(), date_to.date(), self.max_window_days
        ):
            page = 1
            while True:
                params = {
                    # ASSUMED: plain UTC window, mirroring queryTransactionByUid's
                    # live-verified behaviour (module docstring); not independently
                    # live-verified for queryMyCommission itself.
                    "rebateStartAt": to_millis(window_start),
                    "rebateEndAt": to_millis(window_end),
                    "page": page,
                    "pageSize": _PAGE_SIZE,
                }
                query = urlencode(params)
                endpoint = f"{_MY_COMMISSION_PATH}?{query}"
                payload = await self._send(
                    "GET",
                    endpoint,
                    headers=self._headers("GET", endpoint),
                    log_path=_MY_COMMISSION_PATH,
                )
                self._check_envelope(payload)
                items = self._extract_items(payload)
                for item in items:
                    # VERIFIED (docs): each record carries `commission` + `currency`;
                    # only USDT is summed for the downline total (base contract).
                    if str(item.get("currency")) == "USDT":
                        total += parse_decimal(item["commission"])

                if len(items) < _PAGE_SIZE:
                    break
                page += 1
        return total

    async def verify_referral(self, uid: str) -> ReferralCheck:
        """Query the invitee list for ``uid`` (no date filter -> full membership).

        VERIFIED (docs): ``queryInvitees`` accepts a ``uid`` filter and returns a
        record for a referred user regardless of commission — a $0-trading referral
        still appears. A matching record confirms; an empty result is an
        authoritative non-referral, so absence rejects.
        """
        query = urlencode({"uid": uid, "page": 1, "pageSize": _PAGE_SIZE})
        endpoint = f"{_INVITEES_PATH}?{query}"
        payload = await self._send(
            "GET", endpoint, headers=self._headers("GET", endpoint), log_path=_INVITEES_PATH
        )
        self._check_envelope(payload)
        for item in self._extract_items(payload):
            if str(item.get("uid")) == uid:
                return ReferralCheck.CONFIRMED
        return ReferralCheck.NOT_REFERRAL

    def _check_envelope(self, payload: object) -> None:
        if isinstance(payload, dict) and str(payload.get("code")) not in ("200000", "None"):
            raise ExchangeApiError(f"{self.name}: {payload.get('code')} {payload.get('msg')}")

    @staticmethod
    def _extract_items(payload: object) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                return items
        return []


@register("kucoin")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> KucoinAdapter:
    return KucoinAdapter(credentials, settings)
