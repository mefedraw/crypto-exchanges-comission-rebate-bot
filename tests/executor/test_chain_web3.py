from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound

from bot.executor.chain import ChainError, MockChainClient
from bot.executor.chain_web3 import (
    Web3ChainClient,
    build_chain_client,
    build_mantle_chain_client,
)

# A valid (throwaway) private key + addresses for offline signing/encoding. NOT a real key.
_PK = "0x" + "11" * 32
_USDT = "0x" + "ab" * 20
_TO = "0x" + "cd" * 20
_TRANSFER_SELECTOR = "0xa9059cbb"  # keccak("transfer(address,uint256)")[:4]


class _FakeEth:
    """Minimal stand-in for AsyncWeb3.eth with the async surface Web3ChainClient uses."""

    def __init__(
        self,
        *,
        gas_price: int = 10**9,
        nonce_pending: int = 0,
        nonce_latest: int = 0,
        receipt: dict | None = None,
        block: int = 0,
    ) -> None:
        self._gas_price = gas_price
        self._nonce_pending = nonce_pending
        self._nonce_latest = nonce_latest
        self._receipt = receipt
        self._block = block
        self.send_raw_transaction = AsyncMock(return_value=b"")

    @property
    def gas_price(self):  # web3 exposes this as an awaitable property
        return self._agp()

    async def _agp(self) -> int:
        return self._gas_price

    async def get_transaction_count(self, address: str, block: str) -> int:
        return self._nonce_pending if block == "pending" else self._nonce_latest

    async def get_transaction_receipt(self, txhash: str) -> dict:
        if self._receipt is None:
            raise TransactionNotFound(txhash)
        return self._receipt

    @property
    def block_number(self):
        return self._ablock()

    async def _ablock(self) -> int:
        return self._block


class _FakeW3:
    def __init__(self, eth: _FakeEth) -> None:
        self.eth = eth

    @staticmethod
    def to_checksum_address(addr: str) -> str:
        return AsyncWeb3.to_checksum_address(addr)

    @staticmethod
    def to_hex(value: object) -> str:
        return AsyncWeb3.to_hex(value)


def _client(**eth_kwargs) -> Web3ChainClient:
    c = Web3ChainClient(
        rpc_urls=["http://localhost:8545"], private_key=_PK, usdt_contract=_USDT, chain_id=97
    )
    c._w3 = _FakeW3(_FakeEth(**eth_kwargs))  # type: ignore[assignment]
    return c


def _native_client(**eth_kwargs) -> Web3ChainClient:
    """A Mantle-shaped client: no usdt_contract -> native mode (send_native only)."""
    c = Web3ChainClient(
        rpc_urls=["http://localhost:8545"], private_key=_PK, usdt_contract=None, chain_id=5000
    )
    c._w3 = _FakeW3(_FakeEth(**eth_kwargs))  # type: ignore[assignment]
    return c


# --- factory ---


def test_build_chain_client_falls_back_to_mock_without_creds():
    settings = SimpleNamespace(
        payout_private_key=None,
        bsc_rpc_urls=[],
        usdt_contract=None,
        bsc_chain_id=56,
        max_gas_price_wei=None,
        bnb_usd_feed=None,
    )
    assert isinstance(build_chain_client(settings), MockChainClient)  # type: ignore[arg-type]


def test_build_chain_client_builds_web3_with_creds():
    settings = SimpleNamespace(
        payout_private_key=SecretStr(_PK),
        bsc_rpc_urls=["http://localhost:8545"],
        usdt_contract=_USDT,
        bsc_chain_id=97,
        max_gas_price_wei=None,
        bnb_usd_feed=None,
    )
    assert isinstance(build_chain_client(settings), Web3ChainClient)  # type: ignore[arg-type]


# --- money-critical: calldata selector + amount is 18-decimal wei, no rescale ---


def test_transfer_calldata_selector_and_amount():
    c = Web3ChainClient(rpc_urls=["http://x"], private_key=_PK, usdt_contract=_USDT)
    data = c._contract.encode_abi("transfer", args=[AsyncWeb3.to_checksum_address(_TO), 10**18])
    assert data.startswith(_TRANSFER_SELECTOR)
    # last 32 bytes (64 hex) encode the amount — must equal the wei we passed, unscaled
    assert int(data[-64:], 16) == 10**18
    # the address argument (bytes 4..36) contains the recipient's 20 bytes, right-aligned
    assert "cd" * 20 in data.lower()


# --- send_transfer ---


@pytest.mark.asyncio(loop_scope="session")
async def test_send_transfer_signs_broadcasts_and_returns_hash():
    c = _client(gas_price=10**9)
    txhash = await c.send_transfer(to=_TO, amount_wei=10**18, nonce=3)
    assert txhash.startswith("0x") and len(txhash) == 66
    c._w3.eth.send_raw_transaction.assert_awaited_once()  # type: ignore[attr-defined]
    assert c._signed[3] == txhash  # remembered before broadcast (recovery)


# --- gas quote (#2): gas cost -> USDT via the Chainlink BNB/USD feed ---


class _FeedCall:
    def __init__(self, ret: object) -> None:
        self._ret = ret

    async def call(self) -> object:
        return self._ret


class _FeedFns:
    def __init__(self, answer: int, decimals: int) -> None:
        self._answer = answer
        self._decimals = decimals

    def latestRoundData(self) -> _FeedCall:  # noqa: N802 - mirrors the on-chain name
        return _FeedCall([0, self._answer, 0, 0, 0])

    def decimals(self) -> _FeedCall:
        return _FeedCall(self._decimals)


class _FakeFeed:
    def __init__(self, *, answer: int, decimals: int = 8) -> None:
        self.functions = _FeedFns(answer, decimals)


@pytest.mark.asyncio(loop_scope="session")
async def test_quote_gas_usdt_from_feed():
    from decimal import Decimal

    c = _client(gas_price=10**9)  # 1 gwei
    c._feed = _FakeFeed(answer=600 * 10**8)  # BNB/USD = 600, 8 decimals
    quote = await c.quote_gas_usdt(120_000)
    assert quote is not None
    assert quote.bnb_usdt_rate == Decimal(600)
    assert quote.gas_bnb == Decimal("0.00012")  # 1e9 * 120000 / 1e18
    assert quote.gas_usdt == Decimal("0.07200000")  # 0.00012 * 600


@pytest.mark.asyncio(loop_scope="session")
async def test_quote_gas_usdt_none_without_feed():
    c = _client(gas_price=10**9)  # no feed configured
    assert await c.quote_gas_usdt(120_000) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_send_transfer_fails_closed_above_gas_cap():
    c = Web3ChainClient(
        rpc_urls=["http://x"],
        private_key=_PK,
        usdt_contract=_USDT,
        max_gas_price_wei=10**9,
    )
    c._w3 = _FakeW3(_FakeEth(gas_price=10**12))  # type: ignore[assignment]
    with pytest.raises(ChainError):
        await c.send_transfer(to=_TO, amount_wei=10**18, nonce=0)
    c._w3.eth.send_raw_transaction.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio(loop_scope="session")
async def test_send_transfer_wraps_broadcast_error_as_chainerror():
    c = _client(gas_price=10**9)
    c._w3.eth.send_raw_transaction = AsyncMock(side_effect=TimeoutError("rpc down"))  # type: ignore[attr-defined]
    with pytest.raises(ChainError):
        await c.send_transfer(to=_TO, amount_wei=10**18, nonce=7)
    # even on broadcast failure, the hash was remembered (so reconcile can recover it)
    assert 7 in c._signed


# --- reads ---


@pytest.mark.asyncio(loop_scope="session")
async def test_get_nonce_uses_pending():
    c = _client(nonce_pending=9)
    assert await c.get_nonce("0x" + "ee" * 20) == 9


@pytest.mark.asyncio(loop_scope="session")
async def test_status_and_confirmations():
    c = _client(receipt={"status": 1, "blockNumber": 100}, block=114)
    assert await c.get_tx_status("0xdead") == "success"
    assert await c.get_confirmations("0xdead") == 15  # 114 - 100 + 1

    c_fail = _client(receipt={"status": 0, "blockNumber": 100}, block=101)
    assert await c_fail.get_tx_status("0xdead") == "failed"

    c_none = _client(receipt=None)
    assert await c_none.get_tx_status("0xdead") == "unknown"
    assert await c_none.get_confirmations("0xdead") == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_find_tx_by_nonce_recovers_only_when_consumed():
    c = _client(nonce_latest=5)
    c._signed[3] = "0xabc"
    assert await c.find_tx_by_nonce("0x" + "ee" * 20, 3) == "0xabc"  # 5 > 3 -> consumed
    assert await c.find_tx_by_nonce("0x" + "ee" * 20, 5) is None  # 5 <= 5 -> not consumed


# --- Task 9: native transfer (MNT on Mantle) — distinct from the token path above ---


@pytest.mark.asyncio(loop_scope="session")
async def test_send_native_sends_value_directly_no_contract_no_calldata():
    c = _native_client(gas_price=10**9)
    amount_wei = 100 * 10**18
    txhash = await c.send_native(to=_TO, amount_wei=amount_wei, nonce=3)
    assert txhash.startswith("0x") and len(txhash) == 66
    c._w3.eth.send_raw_transaction.assert_awaited_once()  # type: ignore[attr-defined]
    assert c._signed[3] == txhash  # remembered before broadcast (recovery), same as token path


@pytest.mark.asyncio(loop_scope="session")
async def test_send_transfer_rejects_native_mode_client():
    """A client built without a usdt_contract (Mantle-shaped) must never be used for a
    token transfer — defensive fail-closed, since this should be unreachable given how
    the executor picks its client per payout.asset."""
    c = _native_client(gas_price=10**9)
    with pytest.raises(ChainError):
        await c.send_transfer(to=_TO, amount_wei=10**18, nonce=0)


@pytest.mark.asyncio(loop_scope="session")
async def test_send_native_fails_closed_above_gas_cap():
    c = Web3ChainClient(
        rpc_urls=["http://x"],
        private_key=_PK,
        usdt_contract=None,
        chain_id=5000,
        max_gas_price_wei=10**9,
    )
    c._w3 = _FakeW3(_FakeEth(gas_price=10**12))  # type: ignore[assignment]
    with pytest.raises(ChainError):
        await c.send_native(to=_TO, amount_wei=10**18, nonce=0)
    c._w3.eth.send_raw_transaction.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio(loop_scope="session")
async def test_send_native_wraps_broadcast_error_as_chainerror():
    c = _native_client(gas_price=10**9)
    c._w3.eth.send_raw_transaction = AsyncMock(side_effect=TimeoutError("rpc down"))  # type: ignore[attr-defined]
    with pytest.raises(ChainError):
        await c.send_native(to=_TO, amount_wei=10**18, nonce=7)
    # even on broadcast failure, the hash was remembered (so reconcile can recover it)
    assert 7 in c._signed


def test_build_mantle_chain_client_none_when_disabled():
    settings = SimpleNamespace(
        payout_private_key=SecretStr(_PK),
        mnt_enabled=False,
        mantle_rpc_url=None,
        mantle_chain_id=5000,
        mantle_max_gas_price_wei=None,
    )
    assert build_mantle_chain_client(settings) is None  # type: ignore[arg-type]


def test_build_mantle_chain_client_none_without_key():
    settings = SimpleNamespace(
        payout_private_key=None,
        mnt_enabled=True,
        mantle_rpc_url="http://localhost:8545",
        mantle_chain_id=5000,
        mantle_max_gas_price_wei=None,
    )
    assert build_mantle_chain_client(settings) is None  # type: ignore[arg-type]


def test_build_mantle_chain_client_builds_native_web3_when_enabled():
    settings = SimpleNamespace(
        payout_private_key=SecretStr(_PK),
        mnt_enabled=True,
        mantle_rpc_url="http://localhost:8545",
        mantle_chain_id=5000,
        mantle_max_gas_price_wei=None,
    )
    client = build_mantle_chain_client(settings)  # type: ignore[arg-type]
    assert isinstance(client, Web3ChainClient)
    assert client._usdt is None  # native mode: no token contract
    assert client._chain_id == 5000


# --- FINDING 3: Mantle gets its OWN gas-price cap, independent of BSC's ---


def test_build_mantle_chain_client_uses_mantle_specific_gas_cap_not_bsc_cap():
    """A tiny BSC-scale MAX_GAS_PRICE_WEI must not leak into the Mantle client — it
    gets MANTLE_MAX_GAS_PRICE_WEI instead, so a legitimate (much cheaper) Mantle gas
    price is never wrongly blocked by a cap sized for BSC."""
    settings = SimpleNamespace(
        payout_private_key=SecretStr(_PK),
        mnt_enabled=True,
        mantle_rpc_url="http://localhost:8545",
        mantle_chain_id=5000,
        # BSC's cap (unused here) is deliberately tiny to prove it's not the one applied.
        max_gas_price_wei=1,
        mantle_max_gas_price_wei=50_000_000_000,
    )
    client = build_mantle_chain_client(settings)  # type: ignore[arg-type]
    assert isinstance(client, Web3ChainClient)
    assert client._max_gas_price_wei == 50_000_000_000
