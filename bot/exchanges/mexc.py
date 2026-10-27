"""MEXC affiliate commission adapter.

Endpoint: GET /api/v3/rebate/affiliate/commission
Docs: https://www.mexc.com/api-docs/spot-v3/rebate-endpoints

Returns per-invitee rows with spot/etf/futures and a `total` already in USDT.
The `uid` query param filters server-side to a single invitee (verified live).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ReferralCheck
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex
from bot.utils.dates import iter_windows, to_millis
from bot.utils.money import parse_decimal

_PATH = "/api/v3/rebate/affiliate/commission"
# VERIFIED (live): the affiliate's referral list, uid-filterable. A uid that is NOT
# the affiliate's referral returns business code 1035 (success:false, data:null),
# regardless of the date window — an authoritative negative — so verify_referral
# hard-rejects on 1035.
_REFERRAL_PATH = "/api/v3/rebate/affiliate/referral"
_REFERRAL_WINDOW_DAYS = 30
_NOT_REFERRAL_CODE = "1035"  # VERIFIED (live): uid not in the affiliate's network.
_PAGE_SIZE = 100  # VERIFIED (live): page/pageSize pagination, response has totalPage.
# VERIFIED (live): the invitee uid is in the `uid` field. Extra names kept as fallback.
_UID_FIELDS = ("uid", "inviteUid", "inviteeUid", "subUid")
# VERIFIED (live, vs cabinet chart): MEXC buckets commission by UTC+8 day (the
# cabinet's 31.05 daily total matched only a UTC+8-day API query, not UTC). Shift
# the UTC window back 8h to align with MEXC's days (same as Bitget/KuCoin).
_UTC8_OFFSET_MS = 8 * 60 * 60 * 1000


class MexcAdapter(BaseHttpAdapter):
    name = "MEXC"
    code = "mexc"
    base_url = "https://api.mexc.com"  # VERIFIED: MEXC spot REST host.
    # VERIFIED (live): `total` is the per-invitee commission for [startTime, endTime],
    # but it is NOT additive across sub-periods — summing adjacent windows OVER-counts
    # (measured: 3×30d windows = 760.78 vs one 90d call = 720.24). So MEXC must be
    # queried with the WHOLE period in a single request. The user period is capped at
    # 365 days (validate_period) and MEXC accepts a 365-day range, so one window fits.
    max_window_days = 365
    supports_uid_filter = True  # VERIFIED (live): the `uid` query param filters server-side.
    supports_date_range = True

    def _signed_query(self, params: dict[str, object]) -> str:
        """MEXC signing: signature = HMAC-SHA256-hex over the query string."""
        # VERIFIED: spot v3 signs the urlencoded query (incl. timestamp) and
        # appends &signature=...; key goes in the X-MEXC-APIKEY header.
        query = urlencode(params)
        signature = hmac_hex(self._creds.api_secret.get_secret_value(), query)
        return f"{query}&signature={signature}"

    def _headers(self) -> dict[str, str]:
        return {
            "X-MEXC-APIKEY": self._creds.api_key.get_secret_value(),
            "Accept": "application/json",
        }

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        result = self._new_result(uid, date_from, date_to)
        self._log.info("get_commission", uid=uid, date_from=str(date_from), date_to=str(date_to))

        total = Decimal(0)
        for window_start, window_end in iter_windows(
            date_from.date(), date_to.date(), self.max_window_days
        ):
            total += await self._collect_window(uid, window_start, window_end, result)

        result.total_usdt = total
        if total > 0:
            result.add_amount("USDT", total)
        result.settlement_note = "Сумма `total` (USDT) по записям MEXC за период."
        return result.finalize()

    async def _collect_window(
        self, uid: str, window_start: datetime, window_end: datetime, result: CommissionResult
    ) -> Decimal:
        page = 1
        total = Decimal(0)
        while True:
            params = {
                "uid": uid,  # VERIFIED (live): server-side filter to this invitee.
                # Shift UTC boundaries to UTC+8 days to match MEXC's bucketing.
                "startTime": to_millis(window_start) - _UTC8_OFFSET_MS,
                "endTime": to_millis(window_end) - _UTC8_OFFSET_MS,
                "page": page,
                "pageSize": _PAGE_SIZE,
                "timestamp": int(time.time() * 1000),
            }
            request_path = f"{_PATH}?{self._signed_query(params)}"
            payload = await self._send("GET", request_path, headers=self._headers(), log_path=_PATH)
            self._check_envelope(payload)
            rows = self._extract_rows(payload)
            for row in rows:
                # Defensive: keep matching even though the server already filters.
                if self._row_matches_uid(row, uid):
                    total += parse_decimal(row.get("total", "0"))
            result.raw_records_count += len(rows)

            if not rows or page >= self._total_page(payload):
                break
            page += 1
        return total

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        """Whole-downline USDT rebate: same commission endpoint WITHOUT the `uid`
        filter, so MEXC returns every invitee's rows (VERIFIED live: `uid` is an
        optional query param — omitting it is not documented explicitly, but the
        endpoint does not 400/1035 without it and returns rows for all invitees).

        CRITICAL: queried in ONE request spanning the whole period, never sliced into
        sub-windows — MEXC's per-row `total` is NOT additive across sub-periods (see
        the max_window_days note above: 3x30d windows over-counted vs one 90d call).
        max_window_days is already 365 and callers cap periods at 365 days, so a
        single request always covers the whole range; still, this method takes
        date_from/date_to as-is (no iter_windows) so it can never accidentally slice."""
        self._log.info("get_downline_total", date_from=str(date_from), date_to=str(date_to))
        total = Decimal(0)
        page = 1
        while True:
            params: dict[str, object] = {
                # No `uid` here (unlike get_commission): every invitee's rows come back.
                "startTime": to_millis(date_from) - _UTC8_OFFSET_MS,
                "endTime": to_millis(date_to) - _UTC8_OFFSET_MS,
                "page": page,
                "pageSize": _PAGE_SIZE,
                "timestamp": int(time.time() * 1000),
            }
            request_path = f"{_PATH}?{self._signed_query(params)}"
            payload = await self._send("GET", request_path, headers=self._headers(), log_path=_PATH)
            self._check_envelope(payload)
            rows = self._extract_rows(payload)
            for row in rows:
                # VERIFIED (live): `total` is the per-invitee USDT commission field,
                # same as get_commission uses per-uid.
                total += parse_decimal(row.get("total", "0"))

            if not rows or page >= self._total_page(payload):
                break
            page += 1
        return total

    async def verify_referral(self, uid: str) -> ReferralCheck:
        """Ask MEXC (uid-filtered) whether ``uid`` is the affiliate's referral.

        VERIFIED (live), all HTTP 200: code 0 / success -> CONFIRMED; code 1035
        (success:false, data:null) -> NOT_REFERRAL (authoritative "not your
        referral", amount-independent — a non-referral uid gets 1035 regardless of
        window); anything else -> UNVERIFIABLE (fail open). Classifies the code
        directly (``_check_envelope`` would raise on 1035 and bury it).
        """
        end = datetime.now(UTC)
        begin = end - timedelta(days=_REFERRAL_WINDOW_DAYS - 1)
        params: dict[str, object] = {
            "uid": uid,
            "startTime": to_millis(begin),
            "endTime": to_millis(end),
            "page": 1,
            "pageSize": _PAGE_SIZE,
            "timestamp": int(time.time() * 1000),
        }
        request_path = f"{_REFERRAL_PATH}?{self._signed_query(params)}"
        payload = await self._send(
            "GET", request_path, headers=self._headers(), log_path=_REFERRAL_PATH
        )
        if not isinstance(payload, dict):
            return ReferralCheck.UNVERIFIABLE
        code = str(payload.get("code"))
        if code == _NOT_REFERRAL_CODE:
            return ReferralCheck.NOT_REFERRAL
        if code in ("0", "200"):
            return ReferralCheck.CONFIRMED  # a non-referral uid would have returned 1035
        return ReferralCheck.UNVERIFIABLE

    @staticmethod
    def _total_page(payload: object) -> int:
        # VERIFIED (live): pagination metadata lives in data.totalPage.
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                try:
                    return int(data.get("totalPage", 1))
                except (TypeError, ValueError):
                    return 1
        return 1

    def _check_envelope(self, payload: object) -> None:
        # MEXC error envelope: {"code": <non-200>, "msg": "..."}.
        if (
            isinstance(payload, dict)
            and "code" in payload
            and str(payload["code"]) not in ("200", "0")
        ):
            raise ExchangeApiError(f"{self.name}: {payload.get('code')} {payload.get('msg')}")

    @staticmethod
    def _extract_rows(payload: object) -> list[dict]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                # VERIFIED (live): rows live under data.resultList.
                for key in ("resultList", "list", "records", "items"):
                    inner = data.get(key)
                    if isinstance(inner, list):
                        return inner
        return []

    @staticmethod
    def _row_matches_uid(row: dict, uid: str) -> bool:
        return any(str(row.get(field)) == uid for field in _UID_FIELDS)


@register("mexc")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> MexcAdapter:
    return MexcAdapter(credentials, settings)
