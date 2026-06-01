"""Decimal money helpers. Never use float for money.

Parsing tolerates the string amounts exchanges return; formatting trims noise
while preserving precision (no rounding of stored values).
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


class MoneyError(ValueError):
    """Raised when a value cannot be parsed as a decimal amount."""


def parse_decimal(raw: str | int | float | Decimal) -> Decimal:
    """Parse an exchange-supplied amount into :class:`Decimal`.

    Floats are stringified first so we never inherit binary-float error.
    """
    if isinstance(raw, Decimal):
        return raw
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"Cannot parse amount: {raw!r}") from exc


def sum_amounts(amounts: Iterable[Decimal]) -> Decimal:
    """Sum decimals exactly, starting from a Decimal zero."""
    total = Decimal(0)
    for amount in amounts:
        total += amount
    return total


def format_amount(amount: Decimal) -> str:
    """Human-readable amount: fixed-point, trailing zeros trimmed, no exponent.

    Examples: ``Decimal("12.4500") -> "12.45"``, ``Decimal("0E-8") -> "0"``.
    """
    if amount == 0:
        return "0"
    # normalize() can yield exponent notation (e.g. 1E+2); expand it back.
    normalized = amount.normalize()
    sign, digits, exponent = normalized.as_tuple()
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    text = format(normalized, "f")
    return text


def format_2dp(amount: Decimal) -> str:
    """Format an amount with exactly two decimals (for the headline USDT total)."""
    return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"
