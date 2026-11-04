from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from bot.botui import accounts, service
from bot.botui.keyboards import UidExchangeCB, uid_exchange_keyboard
from bot.botui.referrals_catalog import get_referral
from bot.exchanges.registry import build_adapter, build_adapters
from bot.handlers.credentials import _exchange_keyboard
from bot.security.credential_updater import UpdateRejected, validate_request
from tests.conftest import make_settings


def test_pause_normalizes_names_and_preserves_credentials():
    settings = make_settings(
        DISABLED_EXCHANGES=" MEXC,bitget, mexc ,",
        MEXC_API_KEY="key",
        MEXC_API_SECRET="secret",
        BITGET_API_KEY="key",
        BITGET_API_SECRET="secret",
        BITGET_API_PASSPHRASE="p",
        GATE_API_KEY="key",
        GATE_API_SECRET="secret",
    )
    assert settings.disabled_exchanges == {"mexc", "bitget"}
    assert set(settings.available_exchanges) == {"gate"}
    assert settings.mexc_api_key.get_secret_value() == "key"
    assert settings.bitget_api_secret.get_secret_value() == "secret"
    assert build_adapter(settings, "mexc") is None
    assert build_adapter(settings, "bitget") is None


def test_unknown_pause_fails_closed():
    with pytest.raises(ValidationError, match="unknown exchange"):
        make_settings(DISABLED_EXCHANGES="mexcc")


def test_demo_mode_honors_pause_and_removal():
    settings = make_settings(DEMO_MODE=True, DISABLED_EXCHANGES="mexc,bitget")
    assert {"mexc", "bitget", "bitmart"}.isdisjoint(build_adapters(settings))
    assert build_adapter(settings, "bitget") is None
    assert build_adapter(settings, "bitmart") is None


def test_paused_uid_choices_hidden_but_links_preserved(monkeypatch):
    settings = make_settings(DISABLED_EXCHANGES="mexc,bitget")
    monkeypatch.setattr("bot.botui.keyboards.get_settings", lambda: settings)
    choices = {b.callback_data for row in uid_exchange_keyboard().inline_keyboard for b in row}
    for code in ("mexc", "bitget", "bitmart"):
        assert UidExchangeCB(code=code).pack() not in choices
    for code in ("mexc", "bitget"):
        assert get_referral(code).pending
        assert get_referral(code).url
    assert get_referral("bitmart") is None
    admin_choices = {b.text for row in _exchange_keyboard().inline_keyboard for b in row}
    assert "BitMart" not in admin_choices
    assert {"MEXC", "Bitget"}.issubset(admin_choices)


def test_existing_paused_uid_is_visible_and_marked(monkeypatch):
    settings = make_settings(DISABLED_EXCHANGES="mexc,bitget")
    monkeypatch.setattr(accounts, "get_settings", lambda: settings)
    text = accounts._format_list(
        [
            SimpleNamespace(exchange="mexc", uid="ABC"),
            SimpleNamespace(exchange="bitmart", uid="REMOVED"),
        ]
    )
    assert "mexc: ABC" in text
    assert "в ожидании" in text.lower()
    assert "bitmart" not in text
    assert "REMOVED" not in text


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("exchange", ["mexc", "bitget", "bitmart"])
async def test_stale_uid_callback_does_not_start_input(monkeypatch, exchange):
    monkeypatch.setattr(
        accounts, "get_settings", lambda: make_settings(DISABLED_EXCHANGES="mexc,bitget")
    )
    callback = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
    await accounts.choose_exchange(callback, UidExchangeCB(code=exchange), state)
    callback.answer.assert_awaited_once()
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio(loop_scope="session")
async def test_paused_uid_submission_fails_before_verifier_or_database(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: make_settings(DISABLED_EXCHANGES="mexc"))
    verifier = AsyncMock()
    with pytest.raises(service.UidError):
        await service.add_uid(
            None, 123, "mexc", "123", make_settings().rebate_epoch, verifier=verifier
        )
    verifier.assert_not_awaited()


def test_retired_exchange_rejected_by_privileged_updater():
    with pytest.raises(UpdateRejected, match="not allowlisted"):
        validate_request(
            {
                "version": 1,
                "request_id": "00000000-0000-0000-0000-000000000001",
                "actor_id": 123,
                "exchange": "bitmart",
                "values": {},
            }
        )
