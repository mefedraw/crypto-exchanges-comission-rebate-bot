from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from bot.botui import referrals
from bot.botui.keyboards import ReferralCB, referral_menu_keyboard
from bot.botui.referrals_catalog import EMOJI_PACK, LINK_ICON_ID, REFERRALS
from tests.botui.test_referrals import _ctx, _real_message

PACK_ICONS = {
    "5404370482566836590": "💰",  # Bybit, pack position 79
    "5402228118519851497": "💰",  # Binance, position 78
    "5404458795684377671": "💱",  # Bitget, position 73
    "5404871181264269304": "💰",  # Gate, position 77
    "5404332704034496437": "🤑",  # OKX, position 81
    "5404655792949339926": "📈",  # MEXC, position 84
    "5402079538421212517": "📈",  # KuCoin, position 74
    "5404767066962052737": "🔗",  # Neutral link, position 37
}


def test_all_icons_are_from_the_verified_pack_and_have_matching_fallbacks():
    assert EMOJI_PACK == "whalesincryptoio"
    assert len(REFERRALS) == 22
    assert {e.icon_custom_emoji_id for e in REFERRALS} == set(PACK_ICONS)
    for entry in REFERRALS:
        assert entry.icon_emoji == PACK_ICONS[entry.icon_custom_emoji_id]
        if entry.slug not in {"bybit", "binance", "bitget", "gate", "okx", "mexc", "kucoin"}:
            assert entry.icon_custom_emoji_id == LINK_ICON_ID


@pytest.mark.asyncio(loop_scope="session")
async def test_old_aiogram_sends_custom_icon_ids_in_actual_request_serialization():
    bot = Bot("123:abc")
    try:
        payload = bot.session.prepare_value(referral_menu_keyboard(), bot=bot, files={})
        markup = json.loads(payload)
        buttons = [b for row in markup["inline_keyboard"] for b in row]
        for entry in REFERRALS:
            button = next(
                b for b in buttons if b.get("callback_data") == ReferralCB(slug=entry.slug).pack()
            )
            assert button["icon_custom_emoji_id"] == entry.icon_custom_emoji_id
            assert button["text"] == entry.name + (" (в ожидании)" if entry.pending else "")
            assert entry.emoji not in button["text"]
    finally:
        await bot.session.close()


def test_fallback_mode_has_no_custom_fields_or_tags_but_preserves_urls_and_terms():
    markup = referral_menu_keyboard(custom_emoji=False).model_dump(exclude_none=True)
    buttons = [b for row in markup["inline_keyboard"] for b in row]
    for entry in REFERRALS:
        button = next(
            b for b in buttons if b.get("callback_data") == ReferralCB(slug=entry.slug).pack()
        )
        assert "icon_custom_emoji_id" not in button
        assert button["text"].startswith(entry.emoji)
        text = referrals.referral_detail_text(entry.slug, custom_emoji=False)
        assert "<tg-emoji" not in text and entry.emoji in text


@pytest.mark.parametrize("entry", REFERRALS, ids=lambda e: e.slug)
def test_detail_uses_pack_custom_entity_and_its_valid_alternative(entry):
    text = referrals.referral_detail_text(entry.slug)
    assert (
        f'<tg-emoji emoji-id="{entry.icon_custom_emoji_id}">{entry.icon_emoji}</tg-emoji>' in text
    )
    assert f"<code>{entry.ref_code}</code>" in text


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("slug", [None, "bybit"])
async def test_telegram_emoji_rejection_falls_back_once_without_breaking_the_menu(
    monkeypatch, slug
):
    error = TelegramBadRequest(
        method=EditMessageText(chat_id=1, message_id=10, text="test"),
        message="BUTTON_CUSTOM_EMOJI_NOT_ALLOWED",
    )
    show = AsyncMock(side_effect=[error, None])
    monkeypatch.setattr(referrals.panel, "show", show)
    callback = SimpleNamespace(message=_real_message(), answer=AsyncMock())
    if slug is None:
        await referrals.open_referrals(callback, _ctx())
        fallback_markup = show.await_args_list[-1].args[3].model_dump(exclude_none=True)
        assert all(
            "icon_custom_emoji_id" not in b
            for row in fallback_markup["inline_keyboard"]
            for b in row
        )
    else:
        await referrals.show_referral(callback, ReferralCB(slug=slug), _ctx())
        assert "<tg-emoji" not in show.await_args_list[-1].args[2]
    assert show.await_count == 2
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio(loop_scope="session")
async def test_other_telegram_errors_are_not_hidden_or_retried(monkeypatch):
    error = TelegramBadRequest(
        method=EditMessageText(chat_id=1, message_id=10, text="test"), message="chat not found"
    )
    show = AsyncMock(side_effect=error)
    monkeypatch.setattr(referrals.panel, "show", show)
    callback = SimpleNamespace(message=_real_message(), answer=AsyncMock())
    with pytest.raises(TelegramBadRequest, match="chat not found"):
        await referrals.open_referrals(callback, _ctx())
    assert show.await_count == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_fallback_failure_is_not_retried_indefinitely(monkeypatch):
    error = TelegramBadRequest(
        method=EditMessageText(chat_id=1, message_id=10, text="test"),
        message="custom emoji invalid",
    )
    show = AsyncMock(side_effect=error)
    monkeypatch.setattr(referrals.panel, "show", show)
    callback = SimpleNamespace(message=_real_message(), answer=AsyncMock())
    with pytest.raises(TelegramBadRequest):
        await referrals.open_referrals(callback, _ctx())
    assert show.await_count == 2
