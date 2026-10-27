"""Toobit affiliate ("agent") commission adapter.

Endpoints: GET /api/v1/agent/* — Binance-fork style, same signing family as MEXC.
Docs: https://api-docs.toobit.com/api/agent.html

Signing (VERIFIED, docs "Basic Information"): HMAC-SHA256-hex over the query string
(query + body; GET has no body), ms ``timestamp`` required, key in ``X-BB-APIKEY``.
Response wrapper: ``{"code": 200, "msg": "success", "data": {...}}``; paginated data
is ``{"pages", "total", "list": [...]}``. Commission is denominated in USDT only.

Unlike MEXC, ``commissionDataList`` returns HOURLY per-invitee rows whose
``commissionVolume`` IS additive — so we slice the period into <=30-day windows and
sum every row. Max window 30 days; retention 365 days.
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ReferralCheck
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex
from bot.utils.dates import iter_windows, to_millis
from bot.utils.money import parse_decimal

_COMMISSION_PATH = "/api/v1/agent/commissionDataList"
# VERIFIED (docs): membership check by uid; returns the invitee's profile when the
# uid is under this affiliate, else business code -1153 "User not exist".
_RELATION_PATH = "/api/v1/agent/inviteRelationCheck"
_PAGE_SIZE = 100  # VERIFIED (docs): pageIndex from 1, pageSize 100-200; data.pages total.
_SUCCESS_CODE = "200"  # VERIFIED (docs): success wrapper code.
_USER_NOT_EXIST = "-1153"  # VERIFIED (docs): uid has no invite relation with this affiliate.


class ToobitAdapter(BaseHttpAdapter):
    name = "Toobit"
    code = "toobit"
    base_url = "https://api.toobit.com"  # VERIFIED (docs): REST host.
    # VERIFIED (docs): commissionDataList accepts at most a 30-day window; hourly rows
    # are additive, so slicing + summing is exact per period.
    max_window_days = 30
    supports_uid_filter = True  # VERIFIED (docs): `uid` filters server-side (0 = all).
    supports_date_range = True

    def _signed_query(self, params: dict[str, object]) -> str:
        """Binance-style: signature = HMAC-SHA256-hex over the urlencoded query."""
        query = urlencode(params)
        signature = hmac_hex(self._creds.api_secret.get_secret_value(), query)
        return f"{query}&signature={signature}"

    def _headers(self) -> dict[str, str]:
        return {
            "X-BB-APIKEY": self._creds.api_key.get_secret_value(),
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
        result.settlement_note = "Toobit: сумма commissionVolume (USDT) за период (по данным API)."
        return result.finalize()

    async def _collect_window(
        self, uid: str, window_start: datetime, window_end: datetime, result: CommissionResult
    ) -> Decimal:
        return await self._sum_commission(window_start, window_end, uid=uid, result=result)

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        """Whole-downline USDT rebate: same commissionDataList with ``uid=0`` (all
        invitees), summed over <=30-day windows (VERIFIED docs: uid 0 disables the
        filter). Rows are hourly and additive, so windowed summation is exact."""
        self._log.info("get_downline_total", date_from=str(date_from), date_to=str(date_to))
        total = Decimal(0)
        for window_start, window_end in iter_windows(
            date_from.date(), date_to.date(), self.max_window_days
        ):
            total += await self._sum_commission(window_start, window_end, uid="0", result=None)
        return total

    async def _sum_commission(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        uid: str,
        result: CommissionResult | None,
    ) -> Decimal:
        """Sum ``commissionVolume`` (USDT) across all pages of one <=30-day window.

        ``uid="0"`` means "all invitees" (downline total); a real uid filters to one."""
        page = 1
        total = Decimal(0)
        while True:
            params: dict[str, object] = {
                "uid": uid,
                "startTime": to_millis(window_start),
                "endTime": to_millis(window_end),
                "pageIndex": page,
                "pageSize": _PAGE_SIZE,
                "timestamp": int(time.time() * 1000),
            }
            request_path = f"{_COMMISSION_PATH}?{self._signed_query(params)}"
            payload = await self._send(
                "GET", request_path, headers=self._headers(), log_path=_COMMISSION_PATH
            )
            self._check_envelope(payload)
            rows = self._extract_list(payload)
            for row in rows:
                total += parse_decimal(str(row.get("commissionVolume", "0")))
            if result is not None:
                result.raw_records_count += len(rows)

            if not rows or len(rows) < _PAGE_SIZE or page >= self._total_pages(payload):
                break
            page += 1
        return total

    async def verify_referral(self, uid: str) -> ReferralCheck:
        """Ask Toobit whether ``uid`` is this affiliate's invitee (uid-filtered).

        VERIFIED (docs): ``inviteRelationCheck`` returns the invitee's profile (code
        200) when related; business code -1153 "User not exist" when not — an
        authoritative, amount-independent negative. Anything else / any error ->
        UNVERIFIABLE (fail open; the check is a hygiene gate, not a money gate).
        Classifies the code directly (``_check_envelope`` would raise on -1153)."""
        params: dict[str, object] = {"uid": uid, "timestamp": int(time.time() * 1000)}
        request_path = f"{_RELATION_PATH}?{self._signed_query(params)}"
        try:
            payload = await self._send(
                "GET", request_path, headers=self._headers(), log_path=_RELATION_PATH
            )
        except ExchangeApiError:
            return ReferralCheck.UNVERIFIABLE
        if not isinstance(payload, dict):
            return ReferralCheck.UNVERIFIABLE
        code = str(payload.get("code"))
        if code == _USER_NOT_EXIST:
            return ReferralCheck.NOT_REFERRAL
        if code == _SUCCESS_CODE:
            return ReferralCheck.CONFIRMED  # a non-invitee would have returned -1153
        return ReferralCheck.UNVERIFIABLE

    def _check_envelope(self, payload: object) -> None:
        """Raise on a non-success wrapper code ({"code": <not 200>, "msg": ...})."""
        if (
            isinstance(payload, dict)
            and "code" in payload
            and str(payload["code"]) != _SUCCESS_CODE
        ):
            raise ExchangeApiError(f"{self.name}: {payload.get('code')} {payload.get('msg')}")

    @staticmethod
    def _extract_list(payload: object) -> list[dict[str, object]]:
        """Rows live under data.list (paginated page object)."""
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                inner = data.get("list")
                if isinstance(inner, list):
                    return inner
            if isinstance(data, list):  # defensive: some endpoints return a bare array
                return data
        return []

    @staticmethod
    def _total_pages(payload: object) -> int:
        """Total page count from data.pages (a string per the docs)."""
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                try:
                    return int(data.get("pages", 1))
                except (TypeError, ValueError):
                    return 1
        return 1


@register("toobit")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> ToobitAdapter:
    return ToobitAdapter(credentials, settings)
