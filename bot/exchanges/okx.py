"""OKX affiliate invitee commission adapter (arbitrary date range by uid).

Endpoint: GET /api/v5/affiliate/invitee/list
Docs: https://www.okx.com/docs-v5/en/#affiliate-rest-api

VERIFIED (live, cross-checked against the partner cabinet CSV export): with
``periodType=custom`` + ``begin``/``end`` (Unix ms, UTC) + ``keyword=uid`` the row's
``totalCommission`` is the commission for exactly that window — matched the cabinet export
to 10 decimals on three uids and on an arbitrary mid-month window.

Limits (VERIFIED live):
* window must be <= 90 days, else OKX rejects with ``code=499001`` -> we slice into
  <=90-day windows and sum (commission is additive over disjoint intervals);
* ``begin`` older than ~180 days is NOT rejected — OKX silently returns ``0.0`` — so we
  warn the user rather than trusting the value.

API key needs Affiliate (read) permission only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ReferralCheck
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_base64
from bot.utils.dates import iter_windows, to_millis
from bot.utils.money import parse_decimal

_PATH = "/api/v5/affiliate/invitee/list"
_WINDOW_DAYS = 90  # VERIFIED (live): custom window > 90 days => code 499001.
_LOOKBACK_DAYS = 180  # VERIFIED (live): begin older than this silently returns 0.
# ASSUMED: page-based pagination for the unfiltered (whole-downline) listing, per the
# community-maintained okx-api TS SDK's GetAffiliateInviteeListRequest, which exposes
# `page`/`limit` alongside `periodType`/`begin`/`end`/`keyword` (keyword optional) —
# not independently live-verified here (no real key with >1 page of invitees tested).
_DOWNLINE_PAGE_LIMIT = 100


class OkxAdapter(BaseHttpAdapter):
    name = "OKX"
    code = "okx"
    base_url = "https://www.okx.com"  # VERIFIED: OKX REST host.
    max_window_days = _WINDOW_DAYS
    supports_uid_filter = True  # VERIFIED (live): keyword=uid narrows to the referral.
    supports_date_range = True  # VERIFIED (live): totalCommission per [begin, end].

    def _iso_timestamp(self) -> str:
        # VERIFIED: OKX requires ISO-8601 millis UTC, e.g. 2020-12-08T09:08:57.715Z.
        now = datetime.now(UTC)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _headers(self, method: str, request_path: str) -> dict[str, str]:
        """OKX signing: SIGN = base64(hmac_sha256(ts + method + requestPath + body))."""
        # VERIFIED: signature scheme per OKX v5 auth docs.
        timestamp = self._iso_timestamp()
        payload = f"{timestamp}{method.upper()}{request_path}"
        sign = hmac_base64(self._creds.api_secret.get_secret_value(), payload)
        passphrase = self._creds.passphrase.get_secret_value() if self._creds.passphrase else ""
        return {
            "OK-ACCESS-KEY": self._creds.api_key.get_secret_value(),
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": passphrase,
            "Accept": "application/json",
        }

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        result = self._new_result(uid, date_from, date_to)
        self._log.info("get_commission", uid=uid, date_from=str(date_from), date_to=str(date_to))

        self._warn_if_beyond_lookback(date_from, result)

        found_any = False
        for window_start, window_end in iter_windows(
            date_from.date(), date_to.date(), _WINDOW_DAYS
        ):
            if await self._collect_window(uid, window_start, window_end, result):
                found_any = True

        if not found_any:
            result.notes.append("uid не найден среди рефералов OKX / нет данных за этот период.")

        result.settlement_note = (
            "Сумма начислений OKX за период (по данным API). "
            "Точные выплаты сверяйтесь с кабинетом партнёра."
        )
        return result.finalize()

    async def _collect_window(
        self, uid: str, window_start: datetime, window_end: datetime, result: CommissionResult
    ) -> bool:
        """Query one <=90-day window; add the uid's commission. Return True if the uid
        was present in the response."""
        params = {
            "periodType": "custom",
            "begin": str(to_millis(window_start)),
            "end": str(to_millis(window_end)),
            "keyword": uid,
            "limit": "100",
        }
        query = urlencode(params)
        request_path = f"{_PATH}?{query}"
        payload = await self._send(
            "GET", request_path, headers=self._headers("GET", request_path), log_path=_PATH
        )
        rows = self._extract_rows(payload)
        row = next((r for r in rows if str(r.get("uid")) == uid), None)
        if row is None:
            return False

        result.raw_records_count += 1
        # VERIFIED (live): totalCommission is denominated in USDT.
        amount = row.get("totalCommission")
        if amount not in (None, ""):
            result.add_amount("USDT", parse_decimal(amount))
        return True

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        """Whole-downline USDT rebate via the same ``invitee/list`` endpoint, with NO
        ``keyword`` filter, so every invitee row is returned instead of just one uid.

        VERIFIED (live, see module docstring): with ``periodType=custom`` +
        ``begin``/``end``, a row's ``totalCommission`` is scoped to exactly that
        window (matched the partner cabinet CSV export to 10 decimals) — that check
        was done with ``keyword=uid`` narrowing to a single row. ``keyword`` is
        documented (community OKX v5 TS SDK: ``GetAffiliateInviteeListRequest``) as an
        optional filter on WHICH rows come back, not a switch on how ``totalCommission``
        is computed, so the same per-row window-scoping is assumed to hold for every
        row when ``keyword`` is simply omitted. This step (omitting ``keyword`` to get
        the FULL downline rather than one uid) has not been independently live-checked
        against a real key with more than one referral — flagged here as # ASSUMED.

        Paginates with ``page``/``limit`` (ASSUMED page-based, see
        ``_DOWNLINE_PAGE_LIMIT``) and stops once a page returns fewer than
        ``_DOWNLINE_PAGE_LIMIT`` rows, mirroring the ``len(rows) < page_size`` stop
        condition already used by the other adapters in this codebase.
        """
        self._log.info("get_downline_total", date_from=str(date_from), date_to=str(date_to))
        total = Decimal(0)
        for window_start, window_end in iter_windows(
            date_from.date(), date_to.date(), _WINDOW_DAYS
        ):
            page = 1
            while True:
                params = {
                    "periodType": "custom",
                    "begin": str(to_millis(window_start)),
                    "end": str(to_millis(window_end)),
                    "page": str(page),
                    "limit": str(_DOWNLINE_PAGE_LIMIT),
                }
                query = urlencode(params)
                request_path = f"{_PATH}?{query}"
                payload = await self._send(
                    "GET",
                    request_path,
                    headers=self._headers("GET", request_path),
                    log_path=_PATH,
                )
                rows = self._extract_rows(payload)
                for row in rows:
                    # VERIFIED (live, module docstring): totalCommission is USDT.
                    amount = row.get("totalCommission")
                    if amount not in (None, ""):
                        total += parse_decimal(str(amount))

                if len(rows) < _DOWNLINE_PAGE_LIMIT:
                    break
                page += 1

        return total

    async def verify_referral(self, uid: str) -> ReferralCheck:
        """Look for ``uid`` among OKX invitees over the last <=90-day window.

        Stays soft (found -> CONFIRMED, miss -> UNVERIFIABLE, never a hard reject):
        VERIFIED (live) the ``keyword`` param is ignored server-side (a bogus uid
        returns the same full page as a real one), so OKX gives no authoritative
        "not my referral" signal, and the list is windowed / silently zeroed beyond
        ~180 days. We match ``uid`` client-side among the returned rows.
        """
        end = datetime.now(UTC)
        begin = end - timedelta(days=_WINDOW_DAYS - 1)
        params = {
            "periodType": "custom",
            "begin": str(to_millis(begin)),
            "end": str(to_millis(end)),
            "keyword": uid,
            "limit": "100",
        }
        request_path = f"{_PATH}?{urlencode(params)}"
        payload = await self._send(
            "GET", request_path, headers=self._headers("GET", request_path), log_path=_PATH
        )
        rows = self._extract_rows(payload)
        if any(str(r.get("uid")) == uid for r in rows):
            return ReferralCheck.CONFIRMED
        return ReferralCheck.UNVERIFIABLE

    def _warn_if_beyond_lookback(self, date_from: datetime, result: CommissionResult) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=_LOOKBACK_DAYS)
        if date_from < cutoff:
            result.notes.append(
                f"OKX отдаёт данные не глубже ~{_LOOKBACK_DAYS} дней назад: за более ранние "
                "даты API молча возвращает 0 — значению за этот период доверять нельзя."
            )

    def _extract_rows(self, payload: object) -> list[dict[str, object]]:
        if not isinstance(payload, dict):
            return []
        if str(payload.get("code", "0")) != "0":
            raise ExchangeApiError(f"{self.name}: {payload.get('code')} {payload.get('msg')}")
        data = payload.get("data")
        return data if isinstance(data, list) else []


@register("okx")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> OkxAdapter:
    return OkxAdapter(credentials, settings)
