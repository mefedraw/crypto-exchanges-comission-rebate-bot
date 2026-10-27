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
import time
from email.utils import parsedate_to_datetime
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
_RATE_LIMIT_BACKOFF_BASE_SECONDS = 5.0
_RATE_LIMIT_BACKOFF_CAP_SECONDS = 60.0
_USER_AGENT = "commission-rebate-bot/0.1"


class BaseHttpAdapter(ExchangeAdapter):
    """Base for HTTP exchange adapters. Subclasses set ``base_url`` and sign requests."""

    base_url: str = ""
    min_request_interval_seconds = 0.0

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
        self._request_pacing_lock = asyncio.Lock()
        self._last_request_started_at = 0.0
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
        content: str | None = None,
    ) -> Any:
        """Send a request with retry/backoff and return parsed JSON.

        ``request_path`` is the full path+query to send verbatim (so it matches
        whatever was signed). ``log_path`` is a secret-free path used for logging
        (defaults to the part before ``?``). ``content`` is the raw request body sent
        verbatim (e.g. a signed JSON string for POST endpoints); it must be the exact
        bytes that were signed, so we never let httpx re-serialize it.
        """
        safe_path = log_path or request_path.split("?", 1)[0]

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                await self._pace_request()
                response = await self._client.request(
                    method, request_path, headers=headers, content=content
                )
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
                    await self._backoff(attempt, response=response, rate_limited=True)
                    continue
                raise ExchangeRateLimitError(f"{self.name}: HTTP 429")
            if status >= 500:
                business = self._business_error_body(response)
                if business is not None:
                    # A subclass recognizes this 5xx as a (non-retryable) business
                    # response, not a transient outage — return it for normal parsing
                    # (e.g. WEEX delivers code -1054 "uid not under channel" as HTTP 500).
                    self._log.debug("5xx business response", path=safe_path, status=status)
                    return business
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

    def _business_error_body(self, response: httpx.Response) -> Any | None:
        """Hook: if a 5xx actually carries a business response this adapter handles (not a
        transient outage), return the parsed body so ``_send`` returns it without retrying;
        else ``None`` to treat the 5xx as a retryable outage. Default: ``None``. Overridden
        e.g. by WEEX, which delivers business code -1054 ("uid not under channel") as an
        HTTP 500 that must NOT be retried and buried."""
        return None

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

    async def _pace_request(self) -> None:
        if self.min_request_interval_seconds <= 0:
            return

        async with self._request_pacing_lock:
            elapsed = time.monotonic() - self._last_request_started_at
            delay = self.min_request_interval_seconds - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_started_at = time.monotonic()

    async def _backoff(
        self,
        attempt: int,
        *,
        response: httpx.Response | None = None,
        rate_limited: bool = False,
    ) -> None:
        delay = self._retry_delay(attempt, response=response, rate_limited=rate_limited)
        await asyncio.sleep(delay)

    @staticmethod
    def _retry_delay(
        attempt: int,
        *,
        response: httpx.Response | None = None,
        rate_limited: bool = False,
    ) -> float:
        if response is not None:
            kucoin_reset = response.headers.get("gw-ratelimit-reset")
            if kucoin_reset is not None:
                try:
                    return min(
                        max(float(kucoin_reset) / 1000, 0.0),
                        _RATE_LIMIT_BACKOFF_CAP_SECONDS,
                    )
                except ValueError:
                    pass

            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                        delay = retry_at.timestamp() - time.time()
                    except (TypeError, ValueError, OverflowError):
                        delay = -1.0
                if delay >= 0:
                    return min(delay, _RATE_LIMIT_BACKOFF_CAP_SECONDS)

        if rate_limited:
            rate_limit_delay = _RATE_LIMIT_BACKOFF_BASE_SECONDS * (2.0**attempt)
            return min(rate_limit_delay, _RATE_LIMIT_BACKOFF_CAP_SECONDS)
        retry_delay = _BACKOFF_BASE_SECONDS * (2.0**attempt)
        return min(retry_delay, _BACKOFF_CAP_SECONDS)
