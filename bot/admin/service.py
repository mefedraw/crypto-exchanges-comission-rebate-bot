# bot/admin/service.py
"""Pure business logic for the admin panel, kept DB-free and easy to unit-test.

Money-adjacent state transitions themselves (release/reject/reduce) live in
``bot.admin.repository`` — this module only validates admin-supplied input
before it reaches those functions.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_commission_amount(text: str) -> Decimal | None:
    """Parse an admin-supplied replacement commission amount.

    Accepts a comma or dot decimal separator. Returns ``None`` for anything that
    isn't a non-negative ``Decimal`` (blank input, garbage text, negative numbers).
    The upper bound (must not exceed the payout's original commission) is enforced
    by ``bot.admin.repository.reduce_payout_commission``, not here.
    """
    cleaned = text.strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value < 0:
        return None
    return value


_CLEAR_KEYWORDS = {"clear", "сброс", "off"}
_STATUS_KEYWORDS = {"active", "blocked"}


def parse_user_id(text: str) -> int | None:
    """Parse a Telegram user id from admin input: digits only, strictly positive."""
    cleaned = text.strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    return value if value > 0 else None


def parse_override_percent(text: str) -> Decimal | None:
    """Parse a personal commission-rate override entered as a PERCENT (0-100).

    Admins type a percentage (``2`` = 2%, ``2.5`` = 2.5%, ``0`` = no commission),
    NOT a fraction — see the rate prompt. Returns the stored FRACTION (percent/100,
    e.g. ``2`` -> ``0.02``). Reuses :func:`parse_commission_amount` for the numeric
    parse (comma or dot separator, rejects negatives) and rejects anything above
    ``100``. The fraction is later capped at the rank fee by
    ``bot.payout.commission.effective_fee`` (only-down).
    """
    value = parse_commission_amount(text)
    if value is None or value > 100:
        return None
    return value / Decimal(100)


def parse_rate_command(text: str) -> tuple[str, Decimal | None] | None:
    """Parse ``"<target> <percent>"`` or ``"<target> clear"`` for the rate-override prompt.

    ``target`` is returned verbatim as the raw first token — a Telegram nickname
    (``@nick`` or bare ``nick``) or a numeric user id. It is intentionally NOT
    resolved to a user id here (this module is DB-free); the caller
    (``bot.admin.router.receive_rate``) resolves it via ``_resolve_target``. The
    second token is a PERCENT (0-100); the returned value is the stored fraction
    (percent/100). Returns ``(target, fraction)`` where ``fraction is None`` means
    "clear the override", or ``None`` if the input doesn't parse (wrong token count,
    blank target, or a second token that is neither ``clear`` nor an in-range percent).
    """
    parts = text.split()
    if len(parts) != 2:
        return None
    target = parts[0].strip()
    if not target:
        return None
    if parts[1].strip().lower() in _CLEAR_KEYWORDS:
        return target, None
    fraction = parse_override_percent(parts[1])
    if fraction is None:
        return None
    return target, fraction


def parse_user_status_command(text: str) -> tuple[str, str] | None:
    """Parse ``"<target> active|blocked"`` for the block/unblock prompt.

    ``target`` (a ``@nick``/bare nick or a numeric user id) is returned verbatim;
    the caller resolves it to a user id (admins only have nicknames, not ids)."""
    parts = text.split()
    if len(parts) != 2:
        return None
    target = parts[0].strip()
    status = parts[1].strip().lower()
    if not target or status not in _STATUS_KEYWORDS:
        return None
    return target, status


def parse_reassign_command(text: str) -> tuple[str, str, str] | None:
    """Parse ``"<exchange> <uid> <target>"`` for the UID-reassignment prompt.

    ``target`` (the new owner — a ``@nick``/bare nick or a numeric user id) is
    returned verbatim; the caller resolves it to a user id."""
    parts = text.split()
    if len(parts) != 3:
        return None
    exchange, uid, target = parts[0].strip().lower(), parts[1].strip(), parts[2].strip()
    if not exchange or not uid or not target:
        return None
    return exchange, uid, target


def parse_block_uid_command(text: str) -> tuple[str, str] | None:
    """Parse ``"<exchange> <uid>"`` for the UID-block prompt."""
    parts = text.split()
    if len(parts) != 2:
        return None
    exchange, uid = parts[0].strip().lower(), parts[1].strip()
    if not exchange or not uid:
        return None
    return exchange, uid
