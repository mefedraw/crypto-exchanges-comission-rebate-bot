# tests/botui/test_referrals.py
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, InlineKeyboardMarkup, Message

from bot.botui import referrals
from bot.botui.keyboards import (
    ReferralCB,
    main_menu,
    referral_detail_keyboard,
    referral_menu_keyboard,
)
from bot.botui.referrals_catalog import REFERRALS, get_referral

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _ctx(user_id: int = 1) -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _real_message() -> Message:
    """A minimal real ``Message`` (passes the handlers' ``isinstance`` guard)."""
    return Message.model_construct(message_id=10, chat=Chat.model_construct(id=1, type="private"))


def _button_texts(markup: InlineKeyboardMarkup) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _button_datas(markup: InlineKeyboardMarkup) -> list[str | None]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


# ---- catalog integrity --------------------------------------------------


def test_every_ref_code_is_parsed_from_its_url() -> None:
    """The stated ref code must appear verbatim in its url — guards typos/drift."""
    for entry in REFERRALS:
        assert entry.ref_code in entry.url, f"{entry.slug}: {entry.ref_code!r} not in {entry.url}"


def test_slugs_are_unique_and_nonempty() -> None:
    slugs = [e.slug for e in REFERRALS]
    assert len(slugs) == len(set(slugs)), "duplicate slug"
    assert all(s and not s.isspace() for s in slugs)


def test_catalog_fields_are_present() -> None:
    for e in REFERRALS:
        assert e.name and e.ref_code and e.emoji
        assert e.url.startswith("https://")


def test_fallback_menu_buttons_have_each_exchange_emoji_and_pending_status() -> None:
    texts = _button_texts(referral_menu_keyboard(custom_emoji=False))
    for entry in REFERRALS:
        status = " (в ожидании)" if entry.pending else ""
        assert f"{entry.emoji} {entry.name}{status}" in texts


def test_binance_in_catalog_with_ref_code() -> None:
    entry = get_referral("binance")
    assert entry is not None
    assert entry.name == "Binance"
    assert entry.ref_code == "ERH4KNEX"
    assert "ref=ERH4KNEX" in entry.url


def test_catalog_has_all_expected_exchanges() -> None:
    names = {e.name for e in REFERRALS}
    for expected in ("Bybit", "Bitget", "Gate", "OKX", "Mexc", "Kucoin", "Bitmart", "Weex"):
        assert expected in names


def test_get_referral_known_and_unknown() -> None:
    assert get_referral("bybit") is not None
    assert get_referral("does-not-exist") is None


# ---- keyboards ----------------------------------------------------------


def test_main_menu_has_referrals_button() -> None:
    assert any("Рефералки" in t for t in _button_texts(main_menu()))


def test_referral_menu_lists_every_exchange_plus_back() -> None:
    texts = _button_texts(referral_menu_keyboard())
    for entry in REFERRALS:
        assert any(entry.name in t for t in texts), entry.name
    assert any("В меню" in t for t in texts)


def test_referral_menu_buttons_carry_referral_cb() -> None:
    datas = _button_datas(referral_menu_keyboard())
    for entry in REFERRALS:
        assert ReferralCB(slug=entry.slug).pack() in datas


def test_detail_keyboard_has_both_back_buttons() -> None:
    texts = _button_texts(referral_detail_keyboard())
    assert any("Рефералки" in t for t in texts)
    assert any("В меню" in t for t in texts)


# ---- detail text --------------------------------------------------------


def test_detail_text_has_link_and_tap_to_copy_code() -> None:
    text = referrals.referral_detail_text("bybit")
    assert text is not None
    assert "https://partner.bybit.com/b/39620" in text
    assert "<code>39620</code>" in text  # <code> => tap-to-copy on mobile


def test_detail_text_escapes_ampersand_for_html() -> None:
    text = referrals.referral_detail_text("phemex")
    assert text is not None
    assert "&amp;referralCode=DGAXD9" in text  # raw & would break HTML parse mode
    assert "<code>DGAXD9</code>" in text


def test_detail_text_unknown_slug_is_none() -> None:
    assert referrals.referral_detail_text("nope") is None


# ---- handlers -----------------------------------------------------------


async def test_open_referrals_shows_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    show = AsyncMock()
    monkeypatch.setattr(referrals.panel, "show", show)
    cb = SimpleNamespace(message=_real_message(), answer=AsyncMock())

    await referrals.open_referrals(cb, _ctx())  # type: ignore[arg-type]

    show.assert_awaited_once()
    assert "Реферальные ссылки" in show.call_args.args[2]  # panel.show(state, src, text, kb)
    cb.answer.assert_awaited_once()


async def test_show_referral_known_slug_shows_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    show = AsyncMock()
    monkeypatch.setattr(referrals.panel, "show", show)
    cb = SimpleNamespace(message=_real_message(), answer=AsyncMock())

    await referrals.show_referral(cb, ReferralCB(slug="kucoin"), _ctx())  # type: ignore[arg-type]

    show.assert_awaited_once()
    assert "<code>CX8QDGKW</code>" in show.call_args.args[2]
    cb.answer.assert_awaited_once()


async def test_show_referral_unknown_slug_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    show = AsyncMock()
    monkeypatch.setattr(referrals.panel, "show", show)
    cb = SimpleNamespace(message=_real_message(), answer=AsyncMock())

    await referrals.show_referral(cb, ReferralCB(slug="ghost"), _ctx())  # type: ignore[arg-type]

    show.assert_not_awaited()
    cb.answer.assert_awaited_once()
    assert cb.answer.call_args.kwargs.get("show_alert") is True
