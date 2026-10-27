# bot/botui/referrals_catalog.py
"""Static catalog of the owner's exchange referral links + parsed ref codes.

Separate from ``EXCHANGE_SPECS`` (the 8 accrual-supported exchanges): this is a
share list the owner pulls up in-bot to grab a link or paste a ref code when
registering through an exchange app. Each entry's ``ref_code`` is the value
parsed out of its ``url``; ``tests/botui/test_referrals.py`` asserts the code
appears verbatim in the url so the two can never silently drift.

Extend by appending a :class:`ReferralEntry` — ``slug`` must stay short (it rides
in callback data, capped at 64 bytes) and unique.
"""

from __future__ import annotations

from dataclasses import dataclass

EMOJI_PACK = "whalesincryptoio"
LINK_ICON_ID = "5404767066962052737"


@dataclass(frozen=True)
class ReferralEntry:
    slug: str  # stable id used in ReferralCB callback data — short + unique
    emoji: str
    name: str
    url: str
    ref_code: str  # parsed from url; shown tap-to-copy in the detail screen
    note: str  # rebate %, plus any special terms
    pending: bool = False
    icon_custom_emoji_id: str = LINK_ICON_ID
    icon_emoji: str = "🔗"


# Order = display order in the referral menu. Codes parsed from each url.
REFERRALS: tuple[ReferralEntry, ...] = (
    ReferralEntry(
        "bybit",
        "💰",
        "Bybit",
        "https://partner.bybit.com/b/39620",
        "39620",
        "50%",
        icon_custom_emoji_id="5404370482566836590",
        icon_emoji="💰",
    ),
    ReferralEntry(
        "binance",
        "💰",
        "Binance",
        "https://www.binance.com/register?ref=ERH4KNEX",
        "ERH4KNEX",
        "20%",
        icon_custom_emoji_id="5402228118519851497",
        icon_emoji="💰",
    ),
    ReferralEntry(
        "bitget",
        "💱",
        "Bitget",
        "https://partner.bitget.com/bg/LN5GXY",
        "LN5GXY",
        "60%",
        pending=True,
        icon_custom_emoji_id="5404458795684377671",
        icon_emoji="💱",
    ),
    ReferralEntry(
        "gate",
        "💰",
        "Gate",
        "https://www.gate.com/share/a1crxv9d",
        "a1crxv9d",
        "50%",
        icon_custom_emoji_id="5404871181264269304",
        icon_emoji="💰",
    ),
    ReferralEntry(
        "okx",
        "🤑",
        "OKX",
        "https://www.okx.com/join/39804961",
        "39804961",
        "50%",
        icon_custom_emoji_id="5404332704034496437",
        icon_emoji="🤑",
    ),
    ReferralEntry(
        "mexc",
        "📈",
        "Mexc",
        "https://www.mexc.com/acquisition/custom-sign-up?shareCode=mexc-russianwhal",
        "mexc-russianwhal",
        "50%",
        pending=True,
        icon_custom_emoji_id="5404655792949339926",
        icon_emoji="📈",
    ),
    ReferralEntry(
        "kucoin",
        "📈",
        "Kucoin",
        "https://www.kucoin.com/r/af/CX8QDGKW",
        "CX8QDGKW",
        "70%",
        icon_custom_emoji_id="5402079538421212517",
        icon_emoji="📈",
    ),
    ReferralEntry(
        "bitmart", "🤑", "Bitmart", "https://www.bitmart.com/invite/c5X6VH/en-US", "c5X6VH", "75%"
    ),
    ReferralEntry(
        "weex", "🤩", "Weex", "https://www.weex.com/en/register?vipCode=wzsu", "wzsu", "70%"
    ),
    ReferralEntry("toobit", "🪙", "Toobit", "https://www.toobit.com/t/pya5xN", "pya5xN", "60%"),
    ReferralEntry(
        "asterdex",
        "🪙",
        "AsterDex",
        "https://www.asterdex.com/en/referral/8D113F",
        "8D113F",
        "15% VIP1 · 10% VIP2+",
    ),
    ReferralEntry(
        "extended",
        "🤑",
        "Extended",
        "https://app.extended.exchange/join/ROCKETOFC",
        "ROCKETOFC",
        "20% · для китов индивидуальные условия",
    ),
    ReferralEntry(
        "okx_boost",
        "🤑",
        "OKX Boost",
        "https://web3.okx.com/join/RUSSIANWHALES",
        "RUSSIANWHALES",
        "20% · для китов индивидуальные условия",
    ),
    ReferralEntry(
        "phemex",
        "🪙",
        "Phemex",
        "https://phemex.com/register?group=8815&referralCode=DGAXD9",
        "DGAXD9",
        "60% · group 8815",
    ),
    ReferralEntry(
        "variational",
        "🤑",
        "Variational",
        "https://omni.variational.io/?ref=OMNIWHALES77",
        "OMNIWHALES77",
        "бонус 12% для команды каждую неделю + 3% к поинтам на TGE",
    ),
    ReferralEntry(
        "true_north",
        "💳",
        "True-North",
        "https://app.true-north.xyz/ref/WHALES",
        "WHALES",
        "7% · для китов индивидуальные условия",
    ),
    ReferralEntry(
        "risex",
        "💳",
        "RiseX",
        "https://www.rise.trade/invite/whales",
        "whales",
        "",
    ),
    ReferralEntry(
        "bitbase",
        "😀",
        "BitBase",
        "https://www.bitbase.com/accounts/invite/CO9HBW",
        "CO9HBW",
        "80%",
    ),
    ReferralEntry(
        "bitunix",
        "👍",
        "Bitunix",
        "https://www.bitunix.com/register?vipCode=kpju",
        "kpju",
        "65%",
    ),
    ReferralEntry(
        "lbank",
        "🐕",
        "Lbank",
        "https://www.lbank.com/ref/667PI",
        "667PI",
        "80%",
    ),
    ReferralEntry(
        "blofin",
        "😎",
        "Blofin",
        "https://blofin.com/register?referral_code=BqzYl3",
        "BqzYl3",
        "65%",
    ),
    ReferralEntry(
        "qfex",
        "💹",
        "QFEX",
        "https://qfex.com/@rocketinv",
        "rocketinv",
        "30%",
    ),
)

_BY_SLUG: dict[str, ReferralEntry] = {entry.slug: entry for entry in REFERRALS}


def get_referral(slug: str) -> ReferralEntry | None:
    """Return the catalog entry for ``slug``, or ``None`` if unknown."""
    return _BY_SLUG.get(slug)
