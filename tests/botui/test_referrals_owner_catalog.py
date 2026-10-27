"""The owner's supplied list, independent of the accrual adapter registry."""

from html import unescape

import pytest

from bot.botui.keyboards import ReferralCB, referral_menu_keyboard
from bot.botui.referrals import referral_detail_text
from bot.botui.referrals_catalog import REFERRALS, get_referral
from bot.config import EXCHANGE_SPECS

OWNER_CATALOG = (
    ("bybit", "💰", "https://partner.bybit.com/b/39620", "50%"),
    ("binance", "💰", "https://www.binance.com/register?ref=ERH4KNEX", "20%"),
    ("bitget", "💱", "https://partner.bitget.com/bg/LN5GXY", "60%"),
    ("gate", "💰", "https://www.gate.com/share/a1crxv9d", "50%"),
    ("okx", "🤑", "https://www.okx.com/join/39804961", "50%"),
    (
        "mexc",
        "📈",
        "https://www.mexc.com/acquisition/custom-sign-up?shareCode=mexc-russianwhal",
        "50%",
    ),
    ("kucoin", "📈", "https://www.kucoin.com/r/af/CX8QDGKW", "70%"),
    ("weex", "🤩", "https://www.weex.com/en/register?vipCode=wzsu", "70%"),
    ("toobit", "🪙", "https://www.toobit.com/t/pya5xN", "60%"),
    ("asterdex", "🪙", "https://www.asterdex.com/en/referral/8D113F", "15% VIP1 · 10% VIP2+"),
    (
        "extended",
        "🤑",
        "https://app.extended.exchange/join/ROCKETOFC",
        "20% · для китов индивидуальные условия",
    ),
    (
        "okx_boost",
        "🤑",
        "https://web3.okx.com/join/RUSSIANWHALES",
        "20% · для китов индивидуальные условия",
    ),
    (
        "phemex",
        "🪙",
        "https://phemex.com/register?group=8815&referralCode=DGAXD9",
        "60% · group 8815",
    ),
    (
        "variational",
        "🤑",
        "https://omni.variational.io/?ref=OMNIWHALES77",
        "бонус 12% для команды каждую неделю + 3% к поинтам на TGE",
    ),
    (
        "true_north",
        "💳",
        "https://app.true-north.xyz/ref/WHALES",
        "7% · для китов индивидуальные условия",
    ),
    ("risex", "💳", "https://www.rise.trade/invite/whales", ""),
    ("bitbase", "😀", "https://www.bitbase.com/accounts/invite/CO9HBW", "80%"),
    ("bitunix", "👍", "https://www.bitunix.com/register?vipCode=kpju", "65%"),
    ("lbank", "🐕", "https://www.lbank.com/ref/667PI", "80%"),
    ("blofin", "😎", "https://blofin.com/register?referral_code=BqzYl3", "65%"),
    ("qfex", "💹", "https://qfex.com/@rocketinv", "30%"),
)


@pytest.mark.parametrize("slug,emoji,url,note", OWNER_CATALOG)
def test_owner_supplied_catalog_matches_details_and_buttons(slug, emoji, url, note):
    entry = get_referral(slug)
    assert entry is not None
    assert (entry.emoji, entry.url, entry.note) == (emoji, url, note)
    text = referral_detail_text(slug)
    assert entry.icon_emoji in text and url in unescape(text)
    assert entry.ref_code in text
    assert bool("В ожидании" in text) == (slug in {"bitget", "mexc"})
    buttons = [b for row in referral_menu_keyboard().inline_keyboard for b in row]
    button = next(b for b in buttons if b.callback_data == ReferralCB(slug=slug).pack())
    assert button.text.startswith(entry.name)
    assert button.model_dump()["icon_custom_emoji_id"] == entry.icon_custom_emoji_id
    assert len(button.callback_data.encode()) <= 64


def test_bitmart_is_preserved_and_link_only_exchanges_do_not_add_adapters():
    assert len(REFERRALS) == 22
    assert get_referral("bitmart").url == "https://www.bitmart.com/invite/c5X6VH/en-US"
    new_slugs = {"bitbase", "bitunix", "lbank", "blofin", "qfex"}
    assert new_slugs.isdisjoint(spec.code for spec in EXCHANGE_SPECS)


def test_risex_has_no_invented_percentage_or_dangling_separator():
    text = referral_detail_text("risex")
    assert text.startswith('<b><tg-emoji emoji-id="') and "RiseX</b>\n\n" in text
    assert "%" not in text


def test_pending_exchanges_are_explicit_but_keep_their_links():
    assert {e.slug for e in REFERRALS if e.pending} == {"bitget", "mexc"}
    for slug in ("bitget", "mexc"):
        assert "<s>" in referral_detail_text(slug)
