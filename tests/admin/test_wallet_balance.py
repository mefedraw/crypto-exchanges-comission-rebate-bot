from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.admin.wallet_balance import (
    WalletBalanceReader,
    build_bsc_balance_reader,
    build_mantle_balance_reader,
)

ADDRESS = "0x" + "12" * 20
TOKEN = "0x" + "ab" * 20


def _settings(**overrides):
    fields = {
        "payout_wallet_address": ADDRESS,
        "bsc_rpc_urls": ["https://example.org"],
        "usdt_contract": TOKEN,
        "bsc_chain_id": 56,
        "mantle_rpc_url": "https://example.org",
        "mantle_chain_id": 5000,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_factories_need_only_public_settings():
    settings = _settings()  # no private-key attribute, not even an empty one
    bsc = build_bsc_balance_reader(settings)
    mantle = build_mantle_balance_reader(settings)
    assert bsc.address == ADDRESS
    assert mantle.address == ADDRESS
    assert not hasattr(bsc, "send_transfer")
    assert not hasattr(bsc, "send_native")
    assert not hasattr(bsc, "_acct")


@pytest.mark.parametrize("field", ["payout_wallet_address", "bsc_rpc_urls", "usdt_contract"])
def test_bsc_missing_public_settings(field):
    assert build_bsc_balance_reader(_settings(**{field: None})) is None


@pytest.mark.parametrize("field", ["payout_wallet_address", "mantle_rpc_url"])
def test_mantle_missing_public_settings(field):
    assert build_mantle_balance_reader(_settings(**{field: None})) is None


def _reader(chain=56):
    reader = WalletBalanceReader(
        rpc_url="https://example.org", address=ADDRESS, chain_id=chain, token=TOKEN
    )
    reader._provider.disconnect = AsyncMock()

    class Eth:
        @property
        def chain_id(self):
            return AsyncMock(return_value=56)()

    eth = Eth()
    eth.get_balance = AsyncMock(return_value=1234567890123456789)
    call = AsyncMock(return_value=9876543210123456789)
    contract = Mock()
    contract.functions.balanceOf.return_value.call = call
    eth.contract = Mock(return_value=contract)
    reader._w3 = SimpleNamespace(eth=eth)
    return reader, eth, call


@pytest.mark.asyncio(loop_scope="session")
async def test_reader_preserves_decimal_precision_and_closes():
    reader, eth, call = _reader()
    assert await reader.native_balance() == Decimal("1.234567890123456789")
    assert await reader.usdt_balance() == Decimal("9.876543210123456789")
    eth.get_balance.assert_awaited_once_with(ADDRESS)
    call.assert_awaited_once()
    await reader.aclose()
    reader._provider.disconnect.assert_awaited_once()


@pytest.mark.asyncio(loop_scope="session")
async def test_wrong_chain_is_rejected_before_balance_read():
    reader, eth, call = _reader(chain=5000)
    with pytest.raises(ValueError, match="unexpected chain"):
        await reader.native_balance()
    with pytest.raises(ValueError, match="unexpected chain"):
        await reader.usdt_balance()
    eth.get_balance.assert_not_awaited()
    call.assert_not_awaited()


@pytest.mark.asyncio(loop_scope="session")
async def test_rpc_errors_propagate_without_zero_fallback():
    reader, eth, _ = _reader()
    eth.get_balance.side_effect = TimeoutError("private rpc endpoint")
    with pytest.raises(TimeoutError):
        await reader.native_balance()
