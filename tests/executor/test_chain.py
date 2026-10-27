# tests/executor/test_chain.py
from __future__ import annotations

from decimal import Decimal

import pytest

from bot.executor.chain import ChainError, MockChainClient, native_to_wei, usdt_to_wei


def test_usdt_to_wei_18_decimals_not_6():
    assert usdt_to_wei(Decimal("1")) == 10**18
    assert usdt_to_wei(Decimal("0.01")) == 10**16
    assert usdt_to_wei(Decimal("45")) == 45 * 10**18


def test_usdt_to_wei_rounds_down_to_cents():
    # business rule: floor to 2dp, THEN scale
    assert usdt_to_wei(Decimal("1.239")) == 123 * 10**16  # 1.23
    assert usdt_to_wei(Decimal("0.019")) == 1 * 10**16  # 0.01


def test_native_to_wei_defaults_to_18_decimals_like_mnt():
    assert native_to_wei(Decimal("1")) == 10**18
    assert native_to_wei(Decimal("100")) == 100 * 10**18
    assert native_to_wei(Decimal("0.01")) == 10**16


def test_native_to_wei_rounds_down_to_cents():
    assert native_to_wei(Decimal("1.239")) == 123 * 10**16  # 1.23
    assert native_to_wei(Decimal("0.019")) == 1 * 10**16  # 0.01


def test_native_to_wei_honors_explicit_decimals():
    assert native_to_wei(Decimal("1"), decimals=6) == 10**6


@pytest.mark.asyncio(loop_scope="session")
async def test_mock_send_native_is_recorded_separately_from_token_send():
    chain = MockChainClient(nonce=5)
    txh = await chain.send_native(to="0xrecipient", amount_wei=100 * 10**18, nonce=5)
    assert await chain.get_tx_status(txh) == "success"
    assert len(chain.native_sent_calls) == 1
    assert chain.native_sent_calls[0]["amount_wei"] == 100 * 10**18
    assert chain.sent_calls == []  # a native send must never show up as a token send


@pytest.mark.asyncio(loop_scope="session")
async def test_mock_send_records_and_reports():
    chain = MockChainClient(nonce=7)
    assert await chain.get_nonce("0xwallet") == 7
    txh = await chain.send_transfer(to="0xrecipient", amount_wei=10**18, nonce=7)
    assert await chain.get_tx_status(txh) == "success"
    assert await chain.find_tx_by_nonce("0xwallet", 7) == txh


@pytest.mark.asyncio(loop_scope="session")
async def test_mock_can_simulate_broadcast_uncertainty():
    # send raises ChainError, but the tx still landed on-chain under the nonce
    chain = MockChainClient(nonce=7, fail_send_but_land=True)
    with pytest.raises(ChainError):
        await chain.send_transfer(to="0xr", amount_wei=10**18, nonce=7)
    # reconcile-by-nonce finds it => proves "sent but unknown" is recoverable
    assert await chain.find_tx_by_nonce("0xwallet", 7) is not None
