"""Real BSC chain client (web3.py) implementing the ChainClient protocol.

This is the deploy-time counterpart to MockChainClient. It is constructed ONLY when a
payout private key + RPC urls are configured; otherwise the executor uses the mock and
performs no on-chain sends. The private key lives only inside this object and is never
logged. BEP20 USDT on BSC has 18 decimals — amounts arrive already in wei from the
executor (usdt_to_wei), so this client never scales again.

Note (documented limitation): `find_tx_by_nonce` recovers the txhash of an in-flight
send only within the SAME process (it caches every hash it signs BEFORE broadcast). A
process restart between signing and DB-persist of the hash would lose that mapping; the
on-chain nonce-consumed check still tells us the tx landed, and a developer alert should
resolve it. Persisting the deterministic hash on the payout BEFORE broadcast (a small
executor refinement) would close that gap entirely — tracked as a follow-up.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from eth_typing import HexStr
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import TransactionNotFound

from bot.executor.chain import (
    DEFAULT_GAS_LIMIT,
    NATIVE_TRANSFER_GAS,
    ChainClient,
    ChainError,
    FundsShortfall,
    GasQuote,
    MockChainClient,
    NativeChainClient,
    OnSigned,
    _coins,
)
from bot.logging import get_logger

if TYPE_CHECKING:
    from bot.config import Settings

logger = get_logger(__name__)

_WEI = Decimal(10) ** 18

# Chainlink AggregatorV3 — only the two reads needed to price BNB in USD.
_AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Minimal ERC20 ABI — only `transfer(address,uint256)` is needed to pay out USDT.
_ERC20_TRANSFER_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]
# Read-only `balanceOf` — used for the admin hot-wallet balance view (no send).
_ERC20_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]
# A BEP20 `transfer` fits comfortably under this; kept fixed (no on-chain estimate needed).
_TRANSFER_GAS = DEFAULT_GAS_LIMIT


class Web3ChainClient:
    """ChainClient backed by a real EVM node over web3.py (AsyncWeb3).

    Doubles as BOTH chains this executor speaks to:
      - USDT on BSC (token mode): construct with ``usdt_contract`` set; use
        ``send_transfer`` (ERC20/BEP20 ``transfer``, gas quoted/deducted in USDT).
      - MNT on Mantle (native mode): construct with ``usdt_contract=None``; use
        ``send_native`` (plain value transfer, no contract, no gas quote — gas is paid
        in native MNT and borne by the wallet owner). A different RPC/chain-id means a
        DIFFERENT nonce sequence even though the signing key (and so the address) is
        the same across both EVM chains.
    """

    def __init__(
        self,
        *,
        rpc_urls: list[str],
        private_key: str,
        usdt_contract: str | None = None,
        chain_id: int = 56,
        max_gas_price_wei: int | None = None,
        bnb_usd_feed: str | None = None,
    ) -> None:
        if not rpc_urls:
            raise ValueError("Web3ChainClient requires at least one RPC url")
        self._w3 = AsyncWeb3(AsyncHTTPProvider(rpc_urls[0]))
        self._fallback_urls = list(rpc_urls[1:])  # reserved for a future failover wrapper
        self._acct = self._w3.eth.account.from_key(private_key)
        self._usdt = self._w3.to_checksum_address(usdt_contract) if usdt_contract else None
        self._chain_id = chain_id
        self._max_gas_price_wei = max_gas_price_wei
        self._contract = (
            self._w3.eth.contract(address=self._usdt, abi=_ERC20_TRANSFER_ABI)
            if self._usdt
            else None
        )
        self._feed = (
            self._w3.eth.contract(
                address=self._w3.to_checksum_address(bnb_usd_feed), abi=_AGGREGATOR_ABI
            )
            if bnb_usd_feed
            else None
        )
        self._feed_decimals: int | None = None
        # nonce -> txhash for txs signed this process, remembered BEFORE broadcast so a
        # post-broadcast RPC timeout is recoverable without a blind resend.
        self._signed: dict[int, str] = {}

    @property
    def address(self) -> str:
        """The sending wallet's public address (derived from the key; never logs the key)."""
        return str(self._acct.address)

    async def native_balance(self) -> Decimal:
        """Hot wallet's native-coin balance (BNB on BSC, MNT on Mantle), in whole coins."""
        wei = await self._w3.eth.get_balance(self._acct.address)
        return Decimal(wei) / Decimal(10**18)

    async def usdt_balance(self) -> Decimal | None:
        """Hot wallet's USDT (BEP20, 18 decimals) balance, or None in native-only mode."""
        if self._usdt is None:
            return None
        contract = self._w3.eth.contract(address=self._usdt, abi=_ERC20_BALANCE_ABI)
        raw = await contract.functions.balanceOf(self._acct.address).call()
        return Decimal(raw) / Decimal(10**18)

    async def funds_shortfall(
        self, *, amount_wei: int, token: bool, gas_limit: int
    ) -> FundsShortfall | None:
        """Live pre-flight against the hot wallet (see ChainReader.funds_shortfall). One
        gas-price read + one or two balance reads; never signs or sends. On any RPC error
        the exception propagates so the caller treats the check as inconclusive and does
        NOT proceed to broadcast blind."""
        gas_price = await self._w3.eth.gas_price
        gas_wei = gas_price * gas_limit
        native_wei = await self._w3.eth.get_balance(self._acct.address)
        if token:
            if self._usdt is None:  # defensive: token check on a native-only client
                return None
            contract = self._w3.eth.contract(address=self._usdt, abi=_ERC20_BALANCE_ABI)
            token_wei = await contract.functions.balanceOf(self._acct.address).call()
            if token_wei < amount_wei:
                return FundsShortfall("amount", _coins(amount_wei), _coins(token_wei))
            if native_wei < gas_wei:  # enough USDT, but not enough BNB for gas
                return FundsShortfall("gas", _coins(gas_wei), _coins(native_wei))
            return None
        need = amount_wei + gas_wei  # native payout covers both the transfer and its gas
        if native_wei < need:
            return FundsShortfall("amount", _coins(need), _coins(native_wei))
        return None

    async def get_nonce(self, address: str) -> int:
        return await self._w3.eth.get_transaction_count(
            self._w3.to_checksum_address(address), "pending"
        )

    async def send_transfer(
        self, *, to: str, amount_wei: int, nonce: int, on_signed: OnSigned | None = None
    ) -> str:
        if self._contract is None or self._usdt is None:
            # Defensive: this instance was built in native mode (no usdt_contract) —
            # never expected to be reached given how the executor picks its client.
            raise ChainError("send_transfer called on a native-mode (no token contract) client")
        gas_price = await self._w3.eth.gas_price
        if self._max_gas_price_wei is not None and gas_price > self._max_gas_price_wei:
            # Fail closed: refuse to overpay rather than send at a spiked gas price.
            raise ChainError(
                f"gas price {gas_price} exceeds MAX_GAS_PRICE_WEI {self._max_gas_price_wei}"
            )
        data = self._contract.encode_abi(
            "transfer", args=[self._w3.to_checksum_address(to), amount_wei]
        )
        tx = {
            "from": self._acct.address,
            "to": self._usdt,
            "value": 0,
            "data": data,
            "nonce": nonce,
            "gas": _TRANSFER_GAS,
            "gasPrice": gas_price,
            "chainId": self._chain_id,
        }
        signed = self._acct.sign_transaction(tx)
        txhash = self._w3.to_hex(signed.hash)  # deterministic — known before broadcast
        self._signed[nonce] = txhash
        # Persist the hash BEFORE broadcasting so a crash in the broadcast window is
        # recoverable from the DB alone (no reliance on this process's memory).
        if on_signed is not None:
            await on_signed(txhash)
        try:
            await self._w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as exc:  # broadcast/RPC uncertainty — caller must not blind-resend
            raise ChainError(f"broadcast failed for nonce {nonce}: {type(exc).__name__}") from exc
        return txhash

    async def send_native(
        self, *, to: str, amount_wei: int, nonce: int, on_signed: OnSigned | None = None
    ) -> str:
        """Native-coin value transfer (no contract, no calldata) — MNT on Mantle.

        Gas here is paid in the chain's native coin and borne by the wallet owner:
        unlike ``send_transfer`` there is no USDT gas quote/deduction anywhere in this
        path (see PayoutExecutor.process_next).
        """
        gas_price = await self._w3.eth.gas_price
        if self._max_gas_price_wei is not None and gas_price > self._max_gas_price_wei:
            raise ChainError(
                f"gas price {gas_price} exceeds MAX_GAS_PRICE_WEI {self._max_gas_price_wei}"
            )
        tx = {
            "from": self._acct.address,
            "to": self._w3.to_checksum_address(to),
            "value": amount_wei,
            "nonce": nonce,
            "gas": NATIVE_TRANSFER_GAS,
            "gasPrice": gas_price,
            "chainId": self._chain_id,
        }
        signed = self._acct.sign_transaction(tx)
        txhash = self._w3.to_hex(signed.hash)  # deterministic — known before broadcast
        self._signed[nonce] = txhash
        if on_signed is not None:
            await on_signed(txhash)
        try:
            await self._w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as exc:  # broadcast/RPC uncertainty — caller must not blind-resend
            raise ChainError(f"broadcast failed for nonce {nonce}: {type(exc).__name__}") from exc
        return txhash

    async def get_tx_status(self, txhash: str) -> str:
        try:
            receipt = await self._w3.eth.get_transaction_receipt(HexStr(txhash))
        except TransactionNotFound:
            return "unknown"
        return "success" if receipt["status"] == 1 else "failed"

    async def get_confirmations(self, txhash: str) -> int:
        try:
            receipt = await self._w3.eth.get_transaction_receipt(HexStr(txhash))
        except TransactionNotFound:
            return 0
        latest = await self._w3.eth.block_number
        return max(0, latest - receipt["blockNumber"] + 1)

    async def find_tx_by_nonce(self, address: str, nonce: int) -> str | None:
        # We are the only sender for this key (single-flight). If the account's mined
        # nonce count has passed `nonce`, our tx for that nonce landed. Return the hash we
        # signed this process; the durable hash on the payout (persisted before broadcast,
        # #3) is what survives a restart — reconcile prefers it.
        if not await self.is_nonce_used(address, nonce):
            return None
        return self._signed.get(nonce)

    async def quote_gas_usdt(self, gas_limit: int) -> GasQuote | None:
        """Estimate the transfer's gas cost in USDT via the Chainlink BNB/USD feed.

        Returns None (=> no deduction) if no feed is configured or any read fails
        (e.g. on a testnet without the feed) — gas is never allowed to block a payout.
        """
        if self._feed is None:
            return None
        try:
            gas_price = await self._w3.eth.gas_price
            if self._feed_decimals is None:
                self._feed_decimals = int(await self._feed.functions.decimals().call())
            answer = (await self._feed.functions.latestRoundData().call())[1]
            if answer <= 0:
                return None
            rate = Decimal(answer) / (Decimal(10) ** self._feed_decimals)
            gas_bnb = Decimal(gas_price * gas_limit) / _WEI
            gas_usdt = (gas_bnb * rate).quantize(Decimal("0.00000001"))
            return GasQuote(gas_bnb=gas_bnb, gas_usdt=gas_usdt, bnb_usdt_rate=rate)
        except Exception as exc:  # noqa: BLE001 - pricing must never block a payout
            logger.warning("gas quote failed; no deduction", error=type(exc).__name__)
            return None

    async def is_nonce_used(self, address: str, nonce: int) -> bool:
        # The mined (latest, not pending) nonce count exceeding `nonce` means our tx for
        # that nonce was included on-chain. Independent of this process's memory, so it
        # stays correct across a restart.
        mined = await self._w3.eth.get_transaction_count(
            self._w3.to_checksum_address(address), "latest"
        )
        return mined > nonce


def build_chain_client(settings: Settings) -> ChainClient:
    """Return the real client when a key + RPC + contract are configured, else the mock.

    Keeps the executor chain-agnostic: no on-chain sends happen unless the deploy has
    provided PAYOUT_PRIVATE_KEY, BSC_RPC_URLS and USDT_CONTRACT.
    """
    if settings.payout_private_key and settings.bsc_rpc_urls and settings.usdt_contract:
        return Web3ChainClient(
            rpc_urls=settings.bsc_rpc_urls,
            private_key=settings.payout_private_key.get_secret_value(),
            usdt_contract=settings.usdt_contract,
            chain_id=settings.bsc_chain_id,
            max_gas_price_wei=settings.max_gas_price_wei,
            bnb_usd_feed=settings.bnb_usd_feed or None,
        )
    return MockChainClient()


def build_mantle_chain_client(settings: Settings) -> NativeChainClient | None:
    """Return the real Mantle native client when MNT is enabled + a key is configured,
    else ``None`` (MNT payouts stay disabled — ``PayoutExecutor`` fails them defensively).

    Same signing key as the BSC client (one wallet, same 0x address on every EVM
    chain) — only the RPC and chain-id differ, which is what keeps the Mantle nonce
    sequence separate from BSC's.
    """
    rpc_url = settings.mantle_rpc_url
    private_key = settings.payout_private_key
    if not settings.mnt_enabled or not private_key or not rpc_url:
        return None
    return Web3ChainClient(
        rpc_urls=[rpc_url],
        private_key=private_key.get_secret_value(),
        usdt_contract=None,
        chain_id=settings.mantle_chain_id,
        # FINDING 3: Mantle's own cap, NOT the BSC MAX_GAS_PRICE_WEI — the two chains'
        # gas prices are on completely different scales.
        max_gas_price_wei=settings.mantle_max_gas_price_wei,
    )
