"""Bybit affiliate customer info adapter.

Endpoint: GET /v5/user/aff-customer-info
Docs: https://bybit-exchange.github.io/docs/v5/affiliate/affiliate-info

Bybit exposes only rolling 30-day / 365-day commission, NOT an arbitrary date
range, and explicitly states the API is not the source of truth for payouts. We
return the closest rolling figure and warn loudly. API key needs Affiliate
permission only.
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import CommissionResult, ExchangeApiError
from bot.exchanges.base_http import BaseHttpAdapter
from bot.exchanges.registry import register
from bot.exchanges.signing import hmac_hex
from bot.utils.money import parse_decimal

_PATH = "/v5/user/aff-customer-info"
_RECV_WINDOW = "5000"


class BybitAdapter(BaseHttpAdapter):
    name = "Bybit"
    code = "bybit"
    base_url = "https://api.bybit.com"  # VERIFIED: Bybit v5 REST host.
    max_window_days = 365
    supports_uid_filter = True
    supports_date_range = False  # VERIFIED: rolling 30/365 only.

    def _headers(self, query: str) -> dict[str, str]:
        """Bybit v5 signing: SIGN = hmac_sha256_hex(ts + apiKey + recvWindow + query)."""
        # VERIFIED: GET signature scheme per Bybit v5 auth docs.
        timestamp = str(int(time.time() * 1000))
        api_key = self._creds.api_key.get_secret_value()
        payload = f"{timestamp}{api_key}{_RECV_WINDOW}{query}"
        sign = hmac_hex(self._creds.api_secret.get_secret_value(), payload)
        return {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": sign,
            "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
            "Accept": "application/json",
        }

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        result = self._new_result(uid, date_from, date_to)
        self._log.info("get_commission", uid=uid, date_from=str(date_from), date_to=str(date_to))

        query = urlencode({"uid": uid})
        payload = await self._send(
            "GET", f"{_PATH}?{query}", headers=self._headers(query), log_path=_PATH
        )
        data = self._extract_result(payload)
        result.raw_records_count = 1 if data else 0

        # Bybit only has rolling 30-day / 365-day windows. Pick whichever window
        # length is closest to the requested span (so a ~1-month query maps to 30d,
        # not 365d). It is still a rolling window, not the exact period — see note.
        period_days = (date_to.date() - date_from.date()).days + 1
        use_30 = abs(period_days - 30) <= abs(period_days - 365)
        chosen_field = "commissions30Day" if use_30 else "commissions365Day"
        self._add_usdt_commission(result, data.get(chosen_field))

        window_label = "30 дней" if use_30 else "365 дней"
        result.notes.append(
            f"Bybit API отдаёт только скользящие 30/365 дней — показано скользящее окно «{window_label}», "
            "а НЕ сумму за выбранный период. Точная сумма — только в Affiliate Portal."
        )
        result.settlement_note = (
            "Bybit: API не является источником истины для выплат — сверяйтесь с Affiliate Portal."
        )
        return result.finalize()

    def _extract_result(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            return {}
        if payload.get("retCode") not in (0, "0", None):
            raise ExchangeApiError(f"{self.name}: {payload.get('retCode')} {payload.get('retMsg')}")
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def _add_usdt_commission(self, result: CommissionResult, value: object) -> None:
        """Record ONLY the USDT commission. Other coins (MNT, USDC, ...) are
        intentionally ignored — by owner's requirement Bybit shows USDT only.

        VERIFIED (live): commissions{30,365}Day is a ``{coin: amount}`` map where
        coins with no commission carry an empty string, e.g.
        ``{"BTC":"", "MNT":"", "USDT":"148.64458062"}``. A list of ``{coin, amount}``
        rows is also accepted defensively.
        """
        usdt = Decimal(0)
        other_coins_present = False

        if isinstance(value, dict):
            for coin, amount in value.items():
                if self._is_usdt(coin):
                    usdt += parse_decimal(amount or 0)  # empty string => 0
                elif self._nonzero(amount):
                    other_coins_present = True
        elif isinstance(value, list):
            # ASSUMED (verify live): [{coin/currency, amount/commission}] rows.
            for row in value:
                if not isinstance(row, dict):
                    continue
                coin = row.get("coin") or row.get("coinName") or row.get("currency")
                amount = row.get("amount") or row.get("commission") or row.get("value")
                if self._is_usdt(coin):
                    usdt += parse_decimal(amount or 0)
                elif self._nonzero(amount):
                    other_coins_present = True
        elif value is not None:
            # Scalar: no per-coin split available. Best effort: treat as USDT.
            usdt = parse_decimal(value)

        if usdt != Decimal(0):
            result.add_amount("USDT", usdt)
        if other_coins_present:
            result.notes.append("Показан только USDT; начисления в других монетах не учтены.")

    @staticmethod
    def _is_usdt(coin: object) -> bool:
        return str(coin).upper() == "USDT"

    @staticmethod
    def _nonzero(amount: object) -> bool:
        try:
            return parse_decimal(amount if amount is not None else 0) != Decimal(0)
        except Exception:  # noqa: BLE001 - defensive; unknown shapes shouldn't crash
            return False


@register("bybit")
def _factory(credentials: ExchangeCredentials, settings: Settings) -> BybitAdapter:
    return BybitAdapter(credentials, settings)
