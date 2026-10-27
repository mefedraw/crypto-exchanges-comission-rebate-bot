# bot/botui/referral.py
"""Referral membership check for the add-UID flow.

Builds the single exchange adapter for the chosen exchange, asks it whether a uid
is one of the owner's referrals, and maps every failure mode to
:data:`ReferralCheck.UNVERIFIABLE` (fail-open) — the check is a hygiene gate, not a
money gate, so it must never block a legitimate add on an API hiccup.
"""

from __future__ import annotations

from bot.config import Settings
from bot.exchanges.base import ExchangeApiError, ReferralCheck
from bot.exchanges.registry import build_adapter
from bot.logging import get_logger

logger = get_logger(__name__)


async def verify_referral(settings: Settings, exchange: str, uid: str) -> ReferralCheck:
    """Return whether ``uid`` is the owner's referral on ``exchange``.

    ``UNVERIFIABLE`` when the exchange has no configured keys, the adapter has no
    membership endpoint, or the API call fails — the caller then adds the uid with
    an "unconfirmed" note instead of rejecting it.
    """
    adapter = build_adapter(settings, exchange)
    if adapter is None:
        return ReferralCheck.UNVERIFIABLE
    try:
        return await adapter.verify_referral(uid)
    except ExchangeApiError as exc:
        # Auth/network/rate-limit/parse errors -> fail open, but leave a trail.
        logger.warning("referral check failed", exchange=exchange, error=type(exc).__name__)
        return ReferralCheck.UNVERIFIABLE
    finally:
        await adapter.aclose()
