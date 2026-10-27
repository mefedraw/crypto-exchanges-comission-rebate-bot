"""Gate (gate.io / gate.com) affiliate commission adapter.

Endpoint: GET /api/v4/rebate/partner/commission_history
Docs: https://www.gate.com/docs/developers/apiv4/en/

The most reliable spec of the seven: a clean list of per-record commissions that
we sum by asset ourselves.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ReferralCheck
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex, sha512_hex
from bot.utils.dates import to_seconds
from bot.utils.money import parse_decimal

_PATH = "/api/v4/rebate/partner/commission_history"
# VERIFIED (official Gate v4 RebateApi docs): the partner's referral graph, keyed by
# user id, independent of any commission — the clean membership check.
_SUB_RELATION_PATH = "/api/v4/rebate/user/sub_relation"
_PAGE_LIMIT = 100  # VERIFIED: Gate accepts limit up to 1000; 100 keeps pages small.


def _query_windows(
    date_from: datetime, date_to: datetime, max_window_days: int
) -> Iterator[tuple[datetime, datetime]]:
    """Split exact instants into inclusive, non-overlapping Gate second windows."""
    if date_from.utcoffset() is None or date_to.utcoffset() is None:
        raise ValueError("Gate query bounds must be timezone-aware.")
    if max_window_days < 1:
        raise ValueError("Gate window size must be positive.")
    start = date_from.astimezone(UTC)
    end = date_to.astimezone(UTC)
    if start > end:
        raise ValueError("Gate query end precedes its start.")
    cursor = start.replace(microsecond=0)
    last = end.replace(microsecond=0)
    while cursor <= last:
        window_end = min(cursor + timedelta(days=max_window_days, seconds=-1), last)
        yield cursor, window_end
        cursor = window_end + timedelta(seconds=1)


class GateAdapter(BaseHttpAdapter):
    name = "Gate"
    code = "gate"
    base_url = "https://api.gateio.ws"  # VERIFIED: Gate v4 REST host.
    max_window_days = 30  # VERIFIED: docs cap the from/to range at 30 days.
    supports_uid_filter = True
    supports_date_range = True
    # ASSUMED: one additional closed day is a conservative buffer for late Gate batches.
    settlement_lag_days = 1

    def _headers(self, method: str, query: str, path: str = _PATH) -> dict[str, str]:
        """Gate v4 signing: SIGN = HMAC-SHA512(method\\npath\\nquery\\nSHA512(body)\\nts)."""
        # VERIFIED (live): signature accepted by Gate (request authenticated; the
        # test key merely lacked the "rebate" permission -> 403 FORBIDDEN).
        timestamp = str(int(time.time()))
        hashed_body = sha512_hex("")  # GET has no body.
        payload = f"{method}\n{path}\n{query}\n{hashed_body}\n{timestamp}"
        signature = hmac_hex(self._creds.api_secret.get_secret_value(), payload, algorithm="sha512")
        return {
            "KEY": self._creds.api_key.get_secret_value(),
            "SIGN": signature,
            "Timestamp": timestamp,
            "Accept": "application/json",
        }

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        result = self._new_result(uid, date_from, date_to)
        self._log.info("get_commission", uid=uid, date_from=str(date_from), date_to=str(date_to))

        for window_start, window_end in _query_windows(date_from, date_to, self.max_window_days):
            await self._collect_window(uid, window_start, window_end, result)

        result.settlement_note = "Сумма начислений по записям Gate за период (по данным API)."
        return result.finalize()

    async def _collect_window(
        self, uid: str, window_start: datetime, window_end: datetime, result: CommissionResult
    ) -> None:
        offset = 0
        while True:
            params = {
                "user_id": uid,
                "from": to_seconds(window_start),
                "to": to_seconds(window_end),
                "limit": _PAGE_LIMIT,
                "offset": offset,
            }
            query = urlencode(params)
            data = await self._send(
                "GET", f"{_PATH}?{query}", headers=self._headers("GET", query), log_path=_PATH
            )
            records = self._extract_records(data)
            for record in records:
                # VERIFIED (live): commission_amount / commission_asset / source
                # (SPOT/FUTURES) confirmed against a real response.
                amount = parse_decimal(record["commission_amount"])
                asset = str(record["commission_asset"])
                source = record.get("source")
                result.add_amount(asset, amount, source=str(source) if source else None)
            result.raw_records_count += len(records)

            if len(records) < _PAGE_LIMIT:
                break
            offset += _PAGE_LIMIT

    async def get_downline_total(self, date_from: datetime, date_to: datetime) -> Decimal | None:
        """Whole-downline USDT commission: same commission_history call WITHOUT the
        user_id filter — Gate then returns every referral's records (VERIFIED: docs
        say "if user_id not specified, all user records are returned"). Sum USDT."""
        total = Decimal(0)
        for window_start, window_end in _query_windows(date_from, date_to, self.max_window_days):
            offset = 0
            while True:
                params = {
                    "from": to_seconds(window_start),
                    "to": to_seconds(window_end),
                    "limit": _PAGE_LIMIT,
                    "offset": offset,
                }
                query = urlencode(params)
                data = await self._send(
                    "GET", f"{_PATH}?{query}", headers=self._headers("GET", query), log_path=_PATH
                )
                records = self._extract_records(data)
                for record in records:
                    if str(record.get("commission_asset")) == "USDT":
                        total += parse_decimal(record["commission_amount"])
                if len(records) < _PAGE_LIMIT:
                    break
                offset += _PAGE_LIMIT
        return total

    async def verify_referral(self, uid: str) -> ReferralCheck:
        """Query the partner referral graph for ``uid``.

        VERIFIED (docs): ``sub_relation`` returns, per queried user id, a ``belong``
        (referral system the user belongs to — empty means none) and ``type`` (0 =
        non-system user). A matching row with non-empty ``belong`` or ``type != 0``
        confirms the referral; an empty result or a ``type == 0`` row is an
        authoritative non-referral (amount-independent), so absence rejects.
        """
        query = urlencode({"user_id_list": uid})
        payload = await self._send(
            "GET",
            f"{_SUB_RELATION_PATH}?{query}",
            headers=self._headers("GET", query, _SUB_RELATION_PATH),
            log_path=_SUB_RELATION_PATH,
        )
        for row in self._extract_records(payload):
            if str(row.get("uid")) != uid:
                continue
            belong = str(row.get("belong") or "").strip()
            try:
                rel_type = int(row.get("type", 0))
            except (TypeError, ValueError):
                rel_type = 0
            if belong or rel_type != 0:
                return ReferralCheck.CONFIRMED
            return ReferralCheck.NOT_REFERRAL
        return ReferralCheck.NOT_REFERRAL

    @staticmethod
    def _extract_records(data: object) -> list[dict[str, Any]]:
        """Gate may return a bare list or a {"list": [...]} envelope."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("list")
            if isinstance(inner, list):
                return inner
        return []


@register("gate")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> GateAdapter:
    return GateAdapter(credentials, settings)
