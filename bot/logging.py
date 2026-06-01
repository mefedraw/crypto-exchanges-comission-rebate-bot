"""Structured logging configured to never leak secrets.

Exposes :func:`configure_logging` (call once at startup) and :func:`mask` for the
rare case where a partial identifier is genuinely useful in a log line.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

# Event-dict keys whose values must never be logged in clear text. Matched
# case-insensitively as substrings, so "access-sign" and "X-BAPI-SIGN" both hit.
_SENSITIVE_KEY_HINTS: tuple[str, ...] = (
    "authorization",
    "api_key",
    "apikey",
    "secret",
    "passphrase",
    "password",
    "token",
    "sign",
    "cookie",
)

_REDACTED = "***REDACTED***"


def mask(value: str, *, visible_tail: int = 4) -> str:
    """Return a maskable identifier as ``****<tail>`` (e.g. ``****a1b2``).

    Use only for values that are safe to partially reveal (never raw secrets).
    """
    if len(value) <= visible_tail:
        return "*" * len(value)
    return "*" * 4 + value[-visible_tail:]


def _redact_sensitive(_: object, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: drop the value of any key that looks sensitive."""
    for key in list(event_dict):
        lowered = key.lower()
        if any(hint in lowered for hint in _SENSITIVE_KEY_HINTS):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(*, level: int = logging.INFO, json_output: bool = True) -> None:
    """Configure structlog + stdlib logging. Idempotent enough for app startup."""
    logging.basicConfig(format="%(message)s", level=level)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_sensitive,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
