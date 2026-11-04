"""Bitunix's separate Partner API (not the public futures trading API).

VERIFIED: supplied Partner OpenAPI document:
https://docs.google.com/document/d/1uNKbWReCeCjAq82I-yjd4-9wGejM-Zxxguwc1O_AUTY/mobilebasic
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ReferralCheck
from bot.exchanges.partner_http import (
    PartnerHttpAdapter,
    amount_of,
    asset_of,
    instant_windows,
    numeric_uid,
    object_of,
    rows_of,
    safe_code,
)
from bot.exchanges.registry import register

_TOTAL_PATH = "/partner/api/v1/openapi/totalTransaction"
_DOWNLINE_PATH = "/partner/api/v1/openapi/totalCommission"
_VERIFY_PATH = "/partner/api/v2/openapi/validateUser"


def partner_signature(params: dict[str, object], secret: str) -> str:
    # VERIFIED: the document's reference implementations sort within each initial
    # character category by the SUM of the key's ASCII codes, not lexicographically.
    # ASSUMED: follow the executable examples where the prose is ambiguous; live
    # reconciliation is still required. None of our fixed parameter keys tie.
    def order(key: str) -> tuple[int, int]:
        category = 0 if key[0].isdigit() else 1 if key[0].islower() else 2
        return category, sum(map(ord, key))

    payload = "".join(str(params[key]) for key in sorted(params, key=order)) + secret
    # VERIFIED: this legacy partner protocol mandates plain SHA1, not HMAC.
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324


class BitunixAdapter(PartnerHttpAdapter):
    name = "Bitunix"
    code = "bitunix"
    base_url = "https://partners.bitunix.com"
    # VERIFIED: 180 days for totalCommission/transaction. ASSUMED: use the same
    # conservative ceiling for the sibling totalTransaction aggregate endpoint.
    max_window_days = 180
    request_interval = 0.35  # VERIFIED: 30 requests per 10 seconds, per endpoint.

    def _prepare_request(
        self,
        method: str,
        request_path: str,
        headers: dict[str, str] | None,
        content: str | None,
    ) -> tuple[str, dict[str, str], str | None]:
        path, _, query = request_path.partition("?")
        params: dict[str, object] = dict(parse_qsl(content if method == "POST" else query))
        # VERIFIED: partner timestamp expires after 30 seconds. A read timeout
        # can already consume that window, so EVERY retry must sign a fresh timestamp.
        params["timestamp"] = int(time.time())
        refreshed = dict(headers or {})
        refreshed["apiKey"] = self._creds.api_key.get_secret_value()
        refreshed["signature"] = partner_signature(
            params, self._creds.api_secret.get_secret_value()
        )
        encoded = urlencode(params)
        if method == "POST":
            return path, refreshed, encoded
        return f"{path}?{encoded}", refreshed, content

    async def _request(
        self, path: str, params: dict[str, object], *, method: str = "GET"
    ) -> dict[str, Any]:
        signed = {**params, "timestamp": int(time.time())}
        headers = {
            "apiKey": self._creds.api_key.get_secret_value(),
            "signature": partner_signature(signed, self._creds.api_secret.get_secret_value()),
        }
        query = urlencode(signed)
        content = None
        request_path = f"{path}?{query}"
        if method == "POST":
            # VERIFIED: validateUser consumes form data, not a JSON body.
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
            request_path, content = path, query
        payload = await self._send(
            method, request_path, headers=headers, content=content, log_path=path
        )
        return self._check_response(payload)

    def _check_response(self, payload: object) -> dict[str, Any]:
        data = object_of(payload)
        if str(data.get("code")) != "0":
            raise ExchangeApiError(f"Bitunix: API error {safe_code(data.get('code'))}")
        if "result" not in data:
            raise ExchangeApiError("Bitunix: missing result")
        return data

    @staticmethod
    def _period(start: datetime, end: datetime) -> dict[str, object]:
        # VERIFIED: ISO UTC seconds in the partner transaction APIs.
        return {
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        uid = numeric_uid(uid)
        result = self._new_result(uid, date_from, date_to)
        for start, end in instant_windows(
            date_from, date_to, self.max_window_days, unit=timedelta(seconds=1)
        ):
            payload = await self._request(
                _TOTAL_PATH, {"uid": uid, "userType": "agent", **self._period(start, end)}
            )
            if payload["result"] is False:
                raise ExchangeApiError("Bitunix: UID не найден в партнёрской структуре.")
            detail = object_of(payload["result"])
            for row in rows_of(detail.get("detail")):
                asset = asset_of(row.get("currency"))
                source = str(row.get("tradeType", "")).upper()
                if source not in {"SPOT", "FUTURES"}:
                    raise ExchangeApiError("Bitunix: invalid tradeType")
                result.add_amount(asset, amount_of(row.get("totalCommission")), source)
                result.raw_records_count += 1
        if result.raw_records_count == 0:
            # An explicitly empty detail list is a successful zero, not an error.
            result.total_usdt = Decimal(0)
        result.notes.append("Bitunix: агрегаты по валютам и spot/futures; точный период в UTC.")
        result.settlement_note = "Bitunix: комиссия партнёра по totalTransaction (по данным API)."
        return result.finalize()

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        total = Decimal(0)
        for start, end in instant_windows(
            date_from, date_to, self.max_window_days, unit=timedelta(seconds=1)
        ):
            payload = await self._request(_DOWNLINE_PATH, self._period(start, end))
            # VERIFIED: omitting uid queries the entire partner hierarchy. This
            # endpoint converts native amounts to USDT at the rate during enquiry.
            total += amount_of(object_of(payload["result"]).get("totalCommission"))
        return total

    async def verify_referral(self, uid: str) -> ReferralCheck:
        uid = numeric_uid(uid)
        payload = await self._request(_VERIFY_PATH, {"account": uid}, method="POST")
        verdict = object_of(payload["result"]).get("result")
        if not isinstance(verdict, bool):
            raise ExchangeApiError("Bitunix: invalid referral verification response")
        # VERIFIED: this endpoint checks DIRECT referrals only. A negative does
        # not authoritatively exclude somebody under a downstream partner.
        return ReferralCheck.CONFIRMED if verdict else ReferralCheck.UNVERIFIABLE


@register("bitunix")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> BitunixAdapter:
    return BitunixAdapter(credentials, settings)
