"""Shared HTTP machinery for exchange adapters.

Provides an httpx-backed base class with:
* TLS on (httpx ``verify=True`` default — never disabled),
* connect/read timeouts,
* exponential backoff retries on network errors, 5xx, and 429 (never on other 4xx),
* mapping of HTTP failures to the :mod:`bot.exchanges.base` error hierarchy,
* structured logging at every step (no secrets, no raw auth headers) so failures
  are traceable after the fact.

Adapters build their own signed request (path + query + headers) and call
:meth:`BaseHttpAdapter._send`; signing stays per-exchange.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from bot.config import ExchangeCredentials, Settings
from bot.exchanges.base import (
    ExchangeAdapter,
    ExchangeApiError,
    ExchangeAuthError,
    ExchangeRateLimitError,
    ExchangeUnavailableError,
)
from bot.logging import get_logger

logger = get_logger(__name__)

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0
_USER_AGENT = "commission-rebate-bot/0.1"


class BaseHttpAdapter(ExchangeAdapter):
    """Base for HTTP exchange adapters. Subclasses set ``base_url`` and sign requests."""

    base_url: str = ""

    def __init__(self, credentials: ExchangeCredentials, settings: Settings) -> None:
        self._creds = credentials
        self._max_retries = max(settings.http_max_retries, 0)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout,
                read=settings.http_read_timeout,
                write=settings.http_read_timeout,
                pool=settings.http_connect_timeout,
            ),
            headers={"User-Agent": _USER_AGENT},
            verify=True,  # TLS verification stays on — do not disable.
        )
        # Bound logger carries the exchange tag on every line; never secrets.
        self._log = logger.bind(exchange=self.code)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self,
        method: str,
        request_path: str,
        *,
        headers: dict[str, str] | None = None,
        log_path: str | None = None,
    ) -> Any:
        """Send a request with retry/backoff and return parsed JSON.

        ``request_path`` is the full path+query to send verbatim (so it matches
        whatever was signed). ``log_path`` is a secret-free path used for logging
        (defaults to the part before ``?``).
        """
        safe_path = log_path or request_path.split("?", 1)[0]

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(method, request_path, headers=headers)
            except UnicodeEncodeError as exc:
                # A credential (key/secret/passphrase) holds a non-ASCII char —
                # commonly a Cyrillic look-alike pasted into .env. Not retryable.
                self._log.warning("non-ascii credential in headers", path=safe_path)
                raise ExchangeAuthError(
                    f"{self.name}: ключ/секрет/passphrase содержит не-ASCII символ "
                    "(возможно кириллица вместо латиницы) — проверьте .env"
                ) from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                self._log.warning("request timeout", path=safe_path, attempt=attempt)
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise ExchangeUnavailableError(f"{self.name}: timeout") from exc
            except httpx.TransportError as exc:
                last_exc = exc
                self._log.warning(
                    "network error", path=safe_path, attempt=attempt, error=type(exc).__name__
                )
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise ExchangeUnavailableError(f"{self.name}: network error") from exc

            status = response.status_code
            if status in (401, 403):
                self._log.warning("auth rejected", path=safe_path, status=status)
                raise ExchangeAuthError(f"{self.name}: HTTP {status}")
            if status == 429:
                self._log.warning("rate limited", path=safe_path, attempt=attempt)
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise ExchangeRateLimitError(f"{self.name}: HTTP 429")
            if status >= 500:
                self._log.warning("server error", path=safe_path, status=status, attempt=attempt)
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise ExchangeUnavailableError(f"{self.name}: HTTP {status}")
            if status >= 400:
                # Other 4xx are not retryable. Let subclasses classify business
                # error codes (e.g. Bitget returns auth failures as HTTP 400).
                self._log.warning("client error", path=safe_path, status=status)
                self._raise_client_error(status, response)

            self._log.debug("request ok", path=safe_path, status=status)
            try:
                return response.json()
            except ValueError as exc:
                raise ExchangeApiError(f"{self.name}: invalid JSON response") from exc

        # Loop only exits via return/raise; this guards against logic errors.
        raise ExchangeUnavailableError(f"{self.name}: request failed") from last_exc

    def _raise_client_error(self, status: int, response: httpx.Response) -> None:
        """Map a non-retryable 4xx into an adapter error.

        Default: a generic :class:`ExchangeApiError` with a trimmed body hint.
        Subclasses override to classify exchange-specific business codes (e.g. an
        auth failure returned as HTTP 400) into :class:`ExchangeAuthError`.
        """
        raise ExchangeApiError(f"{self.name}: HTTP {status}: {response.text[:200]}")

    @staticmethod
    def _safe_json(response: httpx.Response) -> object | None:
        try:
            return response.json()
        except ValueError:
            return None

    async def _backoff(self, attempt: int) -> None:
        delay = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_CAP_SECONDS)
        await asyncio.sleep(delay)
