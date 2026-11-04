"""BitBase.com Partner OpenAPI, as specified in the supplied Single UID PDF.

VERIFIED: Partner Commission User Data Query OpenAPI-Single UID.pdf, pp. 1-11.
Production host, date inclusivity and signature differ from Bitunix's partner API.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ExchangeAuthError, ReferralCheck
from bot.exchanges.partner_http import (
    PartnerHttpAdapter,
    amount_of,
    calendar_windows,
    integer_of,
    numeric_uid,
    object_of,
    rows_of,
    safe_code,
)
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex

_SINGLE_PATH = "/openapi/partner/commission-user"
_LIST_PATH = "/openapi/partner/commission-users"
_PAGE_SIZE = 50


class BitbaseAdapter(PartnerHttpAdapter):
    name = "BitBase"
    code = "bitbase"
    base_url = "https://www.bitbase.com"  # VERIFIED: use production, not the PDF's QA curl host.
    max_window_days = 31  # VERIFIED: inclusive UTC calendar days.
    request_interval = 0.06  # VERIFIED: at most 20 requests/second.

    def _prepare_request(
        self,
        method: str,
        request_path: str,
        headers: dict[str, str] | None,
        content: str | None,
    ) -> tuple[str, dict[str, str], str | None]:
        path, _, query = request_path.partition("?")
        return request_path, self._headers(path, query), content

    def _headers(self, path: str, query: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        api_key = self._creds.api_key.get_secret_value()
        # VERIFIED: exact header order, # delimiters, sorted query without '?'.
        header_text = (
            f"validate-algorithms=HmacSHA256&validate-appkey={api_key}"
            f"&validate-recvwindow=60000&validate-timestamp={timestamp}"
        )
        return {
            "validate-appkey": api_key,
            "validate-timestamp": timestamp,
            "validate-recvwindow": "60000",
            "validate-algorithms": "HmacSHA256",
            "validate-signature": hmac_hex(
                self._creds.api_secret.get_secret_value(), f"{header_text}#GET#{path}#{query}"
            ),
        }

    def _check_response(self, payload: object) -> dict[str, Any]:
        data = object_of(payload)
        code = safe_code(data.get("mc"))
        if str(data.get("rc")) != "0" or code != "SUCCESS":
            if code.startswith("AUTH_") or code == "COSMOS_1012":
                # VERIFIED: COSMOS_1012 also means outside-downline, not just
                # permissions. Never turn it into a definitive negative membership.
                raise ExchangeAuthError(f"BitBase: access rejected ({code})")
            raise ExchangeApiError(f"BitBase: API error {code}")
        return data

    async def _request(self, path: str, params: dict[str, object]) -> dict[str, Any]:
        query = urlencode(sorted(params.items()))
        payload = await self._send(
            "GET", f"{path}?{query}", headers=self._headers(path, query), log_path=path
        )
        return self._check_response(payload)

    @staticmethod
    def _period(start: datetime, end: datetime) -> dict[str, object]:
        return {"startDate": start.date().isoformat(), "endDate": end.date().isoformat()}

    @staticmethod
    def _amounts(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
        total = amount_of(row.get("commissionAmountU"))
        spot = amount_of(row.get("spotCommissionAmountU"))
        futures = amount_of(row.get("futuresCommissionAmountU"))
        # VERIFIED: the PDF explicitly defines this identity. Do not add total to
        # its components or substitute a fee/volume/current-balance field.
        if total != spot + futures:
            raise ExchangeApiError("BitBase: commission total does not match spot + futures")
        return total, spot, futures

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        uid = numeric_uid(uid)
        if date_to.date() > datetime.now(UTC).date():
            raise ExchangeApiError("BitBase: будущие даты недоступны.")
        result = self._new_result(uid, date_from, date_to)
        total = Decimal(0)
        for start, end in calendar_windows(date_from, date_to, self.max_window_days):
            payload = await self._request(
                _SINGLE_PATH, {"userId": uid, "role": "ALL", **self._period(start, end)}
            )
            row = object_of(payload.get("result"))
            if str(row.get("userId")) != uid:
                raise ExchangeApiError("BitBase: response UID does not match request")
            amount, spot, futures = self._amounts(row)
            total += amount
            result.add_amount("USDT", spot, "SPOT")
            result.add_amount("USDT", futures, "FUTURES")
            result.raw_records_count += 1
        result.total_usdt = total
        result.notes.append("BitBase: агрегаты за включительные календарные дни UTC.")
        result.settlement_note = (
            "BitBase: комиссия в USDT по данным Partner API; требуется сверка через кабинет."
        )
        return result.finalize()

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        # VERIFIED: unlike single-UID, the LIST endpoint cannot include UTC today.
        if date_to.date() >= datetime.now(UTC).date():
            return None
        total = Decimal(0)
        for start, end in calendar_windows(date_from, date_to, self.max_window_days):
            seen: set[str] = set()
            page = 1
            expected_count: int | None = None
            while True:
                payload = await self._request(
                    _LIST_PATH,
                    {"role": "ALL", "page": page, "size": _PAGE_SIZE, **self._period(start, end)},
                )
                data = object_of(payload.get("result"))
                count = integer_of(data.get("totalCount"))
                if expected_count is not None and count != expected_count:
                    raise ExchangeApiError("BitBase: downline count changed during pagination")
                expected_count = count
                if count > 10_000:
                    raise ExchangeApiError("BitBase: список превышает лимит 10 000 пользователей.")
                rows = rows_of(data.get("items"))
                for row in rows:
                    uid = numeric_uid(str(row.get("userId", "")))
                    if uid in seen:
                        raise ExchangeApiError("BitBase: repeated user in paginated response")
                    seen.add(uid)
                    total += self._amounts(row)[0]
                if len(seen) == count:
                    break
                if not rows or len(seen) > count or (page + 1) * _PAGE_SIZE > 10_000:
                    raise ExchangeApiError("BitBase: incomplete downline pagination")
                # VERIFIED: pageCount is rows on this page, NOT total pages.
                page += 1
        return total

    async def verify_referral(self, uid: str) -> ReferralCheck:
        uid = numeric_uid(uid)
        today = datetime.now(UTC).date().isoformat()
        try:
            payload = await self._request(
                _SINGLE_PATH, {"userId": uid, "role": "ALL", "startDate": today, "endDate": today}
            )
        except ExchangeAuthError:
            return ReferralCheck.UNVERIFIABLE
        row = object_of(payload.get("result"))
        return (
            ReferralCheck.CONFIRMED if str(row.get("userId")) == uid else ReferralCheck.UNVERIFIABLE
        )


@register("bitbase")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> BitbaseAdapter:
    return BitbaseAdapter(credentials, settings)
