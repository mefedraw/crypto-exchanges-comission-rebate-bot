"""Binance Referral Pro. Performance and payable native commission are distinct."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from time import time_ns
from typing import Any
from urllib.parse import urlencode

import httpx

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ExchangeAuthError, ReferralCheck
from bot.exchanges.partner_http import (
    PartnerHttpAdapter,
    amount_of,
    integer_of,
    numeric_uid,
    object_of,
)
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex

_INVITEE = "/sapi/v1/affiliate/performance/invitee"
_SUCCESS = "000000000"


class _NotInvitee(ExchangeApiError):
    """Binance explicitly rejected this UID's relationship to the authenticated affiliate."""


class BinanceAdapter(PartnerHttpAdapter):
    name = "Binance"
    code = "binance"
    base_url = "https://api.binance.com"
    min_request_interval_seconds = 0.75

    def _prepare_request(
        self, method: str, request_path: str, headers: dict[str, str] | None, content: str | None
    ) -> tuple[str, dict[str, str], None]:
        if method != "GET" or not request_path.startswith(_INVITEE + "?"):
            raise ExchangeApiError("Binance: unexpected performance request")
        path, _, query = request_path.partition("?")
        # VERIFIED: sign the exact query, after pacing and again on each retry.
        query += f"&timestamp={time_ns() // 1_000_000}&recvWindow=5000"
        signature = hmac_hex(self._creds.api_secret.get_secret_value(), query)
        return (
            f"{path}?{query}&signature={signature}",
            {"X-MBX-APIKEY": self._creds.api_key.get_secret_value()},
            None,
        )

    def _raise_client_error(self, status: int, response: httpx.Response) -> None:
        body = self._safe_json(response)
        if isinstance(body, dict):
            code = str(body.get("code"))
            # VERIFIED: only this business error is an authoritative membership rejection.
            if status == 400 and code == "-29002":
                raise _NotInvitee("Binance: invitee does not belong to affiliate (-29002)")
            if code in {"-2014", "-2015", "-1022"}:
                raise ExchangeAuthError("Binance: read key or signature rejected")
        raise ExchangeApiError(f"Binance: performance request rejected (HTTP {status})")

    async def _profile(self, uid: str, period: tuple[date, date] | None) -> dict[str, Any]:
        uid = numeric_uid(uid)
        params = {"userId": uid}
        if period is not None:
            start, end = period
            if start > end:
                raise ValueError("Binance: start date follows end date")
            params.update(startDate=start.isoformat(), endDate=end.isoformat())
        body = object_of(
            await self._send("GET", f"{_INVITEE}?{urlencode(params)}", log_path=_INVITEE)
        )
        if body.get("code") != _SUCCESS or body.get("status") != "OK":
            raise ExchangeApiError("Binance: unsuccessful performance response")
        data = object_of(body.get("data"))
        # VERIFIED: the success response is filtered by userId, not by Link customerId.
        # A membership profile exists independently of activity in the selected period.
        if (
            integer_of(data.get("regTime")) == 0
            or not isinstance(data.get("referralCode"), str)
            or not data["referralCode"].strip()
        ):
            raise ExchangeApiError("Binance: incomplete invitee profile")
        for key in ("userId", "uid", "inviteeUserId"):
            if key in data and str(data[key]) != uid:
                raise ExchangeApiError("Binance: invitee UID mismatch")
        return data

    async def verify_referral(self, uid: str) -> ReferralCheck:
        try:
            await self._profile(uid, None)
        except _NotInvitee:
            return ReferralCheck.NOT_REFERRAL
        return ReferralCheck.CONFIRMED

    async def get_reported_usd(self, uid: str, start: date, end: date) -> Decimal:
        """Dashboard estimate for one explicit interval. Not a wallet movement."""
        data = await self._profile(uid, (start, end))
        amount = amount_of(data.get("commission"))
        if amount < 0:
            raise ExchangeApiError("Binance: negative performance commission")
        return amount

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        # VERIFIED: performance has no asset or distribution status; taxQuery has no
        # invitee UID. Neither can safely produce ledger entries, even for one UID.
        raise ExchangeApiError("Binance: invitee settlement amount and asset unavailable")


@register("binance")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> BinanceAdapter:
    return BinanceAdapter(credentials, settings)
