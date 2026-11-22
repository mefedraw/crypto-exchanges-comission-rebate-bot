"""Binance Referral Pro. Performance and payable native commission are distinct."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
    calendar_windows,
    integer_of,
    numeric_uid,
    object_of,
)
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex

_INVITEE = "/sapi/v1/affiliate/performance/invitee"
_SUCCESS = "000000000"
POLICY = "Binance Referral Pro: 1 USD = 1 USDT (правило бота, не валюта выплаты Binance)."


def _first_possible_day(profile: dict[str, Any]) -> date:
    # Date-only reports do not identify a timezone. UTC-12 is the earliest possible
    # calendar label; do not skip a trade on a boundary by assuming its timezone.
    millis = integer_of(profile["regTime"])
    if profile.get("firstTradeTime"):
        millis = max(millis, integer_of(profile["firstTradeTime"]))
    try:
        return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=millis, hours=-12)).date()
    except OverflowError as exc:
        raise ExchangeApiError("Binance: invalid invitee timestamp") from exc


def _commission(profile: dict[str, Any]) -> Decimal:
    amount = amount_of(profile.get("commission"))
    if amount < 0:
        raise ExchangeApiError("Binance: negative performance commission")
    return amount


class _NotInvitee(ExchangeApiError):
    """Binance explicitly rejected this UID's relationship to the authenticated affiliate."""


class BinanceAdapter(PartnerHttpAdapter):
    name = "Binance"
    code = "binance"
    base_url = "https://api.binance.com"
    min_request_interval_seconds = 0.75
    # Operational buffer: API labels are calendar dates and recent statistics can change.
    settlement_lag_days = 2

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
        return _commission(await self._profile(uid, (start, end)))

    async def accrual_start(self, uid: str, since: date) -> date | None:
        try:
            profile = await self._profile(uid, None)
        except _NotInvitee:
            return None
        if "commission" not in profile and "tradeVol" not in profile:
            # No lifetime metrics yet. Retry membership/metrics next cycle, without
            # recording zeros or preventing another exchange of the same user accruing.
            return None
        _commission(profile)
        return max(since, _first_possible_day(profile))

    async def _same_totals(
        self, uid: str, small: tuple[date, date], large: tuple[date, date]
    ) -> bool:
        a, b = await self._profile(uid, small), await self._profile(uid, large)
        if any("commission" not in p or "tradeVol" not in p for p in (a, b)):
            return False
        return _commission(a) == _commission(b) and amount_of(a["tradeVol"]) == amount_of(
            b["tradeVol"]
        )

    async def _window(self, uid: str, start: date, end: date) -> tuple[Decimal, int]:
        profile = await self._profile(uid, (start, end))
        if "commission" in profile:
            return _commission(profile), 1
        if "tradeVol" in profile:
            raise ExchangeApiError("Binance: volume reported without commission")
        earliest = _first_possible_day(profile)
        if end < earliest:
            return Decimal(0), 0
        # VERIFIED: inactivity can omit both metrics. Accept a zero only with
        # independent, equal dated totals covering exactly the missing interval.
        anchor = max(earliest, end - timedelta(days=self.max_window_days - 1))
        if anchor < start and await self._same_totals(
            uid, (anchor, start - timedelta(days=1)), (anchor, end)
        ):
            return Decimal(0), 0
        closed = datetime.now(UTC).date() - timedelta(days=1 + self.settlement_lag_days)
        after = min(closed, start + timedelta(days=self.max_window_days - 1))
        if end < after and await self._same_totals(
            uid, (end + timedelta(days=1), after), (start, after)
        ):
            return Decimal(0), 0
        raise ExchangeApiError("Binance: no independent confirmation for missing daily metrics")

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        result = self._new_result(numeric_uid(uid), date_from, date_to)
        for start, end in calendar_windows(date_from, date_to, self.max_window_days):
            amount, count = await self._window(uid, start.date(), end.date())
            result.add_amount("USDT", amount, source="Referral Pro USD (1:1)")
            result.raw_records_count += count
        result.settlement_note = POLICY
        return result.finalize()


@register("binance")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> BinanceAdapter:
    return BinanceAdapter(credentials, settings)
