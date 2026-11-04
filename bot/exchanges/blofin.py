"""BloFin daily affiliate commission, restricted to direct invitees.

VERIFIED: https://docs.blofin.com/index.html#get-daily-commission-of-direct-invitees
The daily schema has no currency: BLOFIN_COMMISSION_ASSET must be confirmed by
the operator before registering the adapter; it is never inferred from fee/volume.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError, ExchangeAuthError, ReferralCheck
from bot.exchanges.partner_http import (
    PartnerHttpAdapter,
    amount_of,
    instant_windows,
    integer_of,
    numeric_uid,
    object_of,
    rows_of,
    safe_code,
)
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex
from bot.utils.dates import to_millis

_DAILY_PATH = "/api/v1/affiliate/invitees/daily/info"
_INVITEES_PATH = "/api/v1/affiliate/invitees"
_PAGE_SIZE = 100  # VERIFIED: daily-info limit is at most 100.
_AUTH_CODES = {"152401", "152402", "152404", "152405", "152406", "152408", "152409", "152410"}


class BlofinAdapter(PartnerHttpAdapter):
    name = "BloFin"
    code = "blofin"
    base_url = "https://openapi.blofin.com"
    # ASSUMED: a conservative calendar window, not a documented server maximum.
    # The API returns daily rows; cursor pagination below still handles a full page.
    max_window_days = 30
    request_interval = 0.25  # ASSUMED: conservative local pacing for affiliate reads.

    def __init__(self, credentials: ExchangeCredentials, settings: Settings) -> None:
        super().__init__(credentials, settings)
        self._asset = settings.blofin_commission_asset

    def _prepare_request(
        self,
        method: str,
        request_path: str,
        headers: dict[str, str] | None,
        content: str | None,
    ) -> tuple[str, dict[str, str], str | None]:
        return request_path, self._headers(request_path), content

    def _headers(self, request_path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        nonce = str(uuid4())
        if self._creds.passphrase is None:
            raise ExchangeAuthError("BloFin: missing API passphrase")
        # VERIFIED: base64 of ASCII HEX HMAC, NOT base64 of the raw digest.
        digest = hmac_hex(
            self._creds.api_secret.get_secret_value(), f"{request_path}GET{timestamp}{nonce}"
        )
        return {
            "ACCESS-KEY": self._creds.api_key.get_secret_value(),
            "ACCESS-SIGN": base64.b64encode(digest.encode("ascii")).decode("ascii"),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": self._creds.passphrase.get_secret_value(),
        }

    def _check_response(self, payload: object) -> dict[str, Any]:
        data = object_of(payload)
        code = str(data.get("code"))
        # VERIFIED: current examples mix 200 (invitees) and "0" (daily info).
        if code not in {"0", "200"}:
            if code in _AUTH_CODES or code == "80006":
                raise ExchangeAuthError(f"BloFin: access rejected ({safe_code(code)})")
            raise ExchangeApiError(f"BloFin: API error {safe_code(code)}")
        return data

    async def _request(self, path: str, params: dict[str, object]) -> list[dict[str, Any]]:
        request_path = f"{path}?{urlencode(params)}"
        payload = await self._send(
            "GET", request_path, headers=self._headers(request_path), log_path=path
        )
        return rows_of(self._check_response(payload).get("data"))

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        uid = numeric_uid(uid)
        if self._asset is None:
            raise ExchangeApiError("BloFin: подтвердите валюту в BLOFIN_COMMISSION_ASSET.")
        result = self._new_result(uid, date_from, date_to)
        total = Decimal(0)
        for start, end in instant_windows(
            date_from, date_to, self.max_window_days, unit=timedelta(milliseconds=1)
        ):
            begin, cursor = to_millis(start), to_millis(end)
            seen: set[int] = set()
            while True:
                rows = await self._request(
                    _DAILY_PATH, {"uid": uid, "begin": begin, "end": cursor, "limit": _PAGE_SIZE}
                )
                timestamps: list[int] = []
                for row in rows:
                    stamp = integer_of(row.get("commissionTime"))
                    if str(row.get("uid")) != uid or not begin <= stamp <= cursor:
                        raise ExchangeApiError("BloFin: response UID/date does not match request")
                    if stamp in seen:
                        raise ExchangeApiError("BloFin: repeated daily commission record")
                    seen.add(stamp)
                    timestamps.append(stamp)
                    # VERIFIED: commission is partner income; cashback belongs to
                    # the invitee, while fee is a trading fee. Never sum those fields.
                    total += amount_of(row.get("commission"))
                    result.raw_records_count += 1
                if len(rows) < _PAGE_SIZE:
                    break
                oldest = min(timestamps)
                if oldest == begin:
                    break
                # VERIFIED: data is newest first and the daily API documents
                # begin/end paging. Require progress and never repeat a boundary day.
                cursor = oldest - 1
        # VERIFIED: the daily schema has no tradeType; preserve that absence
        # instead of inventing a spot/futures breakdown from a different endpoint.
        result.add_amount(self._asset, total)
        if self._asset == "USDT":
            result.total_usdt = total
        result.notes.append(
            "BloFin: дневные комиссии только прямых рефералов; точные границы запроса UTC."
        )
        result.settlement_note = (
            "BloFin: сумма commission по данным API; валюта задана в настройках."
        )
        return result.finalize()

    async def verify_referral(self, uid: str) -> ReferralCheck:
        uid = numeric_uid(uid)
        rows = await self._request(_INVITEES_PATH, {"uid": uid, "limit": _PAGE_SIZE})
        if any(str(row.get("uid")) == uid for row in rows):
            return ReferralCheck.CONFIRMED
        # VERIFIED: this list covers direct invitees, not every possible downline
        # relationship. Absence must not reject somebody under a sub-affiliate.
        return ReferralCheck.UNVERIFIABLE


@register("blofin")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> BlofinAdapter:
    return BlofinAdapter(credentials, settings)
