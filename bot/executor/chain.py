# bot/executor/chain.py
"""Chain abstraction for the payout executor. Real web3/RPC lives behind ChainClient
and is added at deploy; this module ships only the money-math and a deterministic mock.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Protocol

_WEI = Decimal(10) ** 18
_CENT = Decimal("0.01")

# Gas units for a BEP20 transfer — the ceiling we send with and charge the user for.
DEFAULT_GAS_LIMIT = 120_000
# A plain native-value transfer (no calldata) — the fixed cost on every EVM chain.
NATIVE_TRANSFER_GAS = 21_000


@dataclass(frozen=True)
class GasQuote:
    """Estimated on-chain cost of one payout transfer, charged to the user."""

    gas_bnb: Decimal  # gas_limit * gas_price, in BNB
    gas_usdt: Decimal  # gas_bnb * bnb_usdt_rate
    bnb_usdt_rate: Decimal  # BNB/USD from the on-chain price feed


@dataclass(frozen=True)
class FundsShortfall:
    """The hot wallet cannot cover a payout. ``kind`` is ``"amount"`` (not enough of the
    coin being paid out) or ``"gas"`` (enough of the coin, but not enough native gas).
    ``need``/``have`` are whole-coin amounts for the human alert; the executor attaches
    the coin symbol (it knows the payout's asset)."""

    kind: str  # "amount" | "gas"
    need: Decimal
    have: Decimal


# Called with the deterministic txhash AFTER signing but BEFORE broadcast, so the
# executor can persist it and survive a crash in the broadcast window (§4.3.1 / #3).
OnSigned = Callable[[str], Awaitable[None]]


def _coins(wei: int) -> Decimal:
    """Base-units (18-decimal wei) -> whole coins, for human-readable funds messages."""
    return Decimal(wei) / _WEI


def usdt_to_wei(amount: Decimal) -> int:
    """USDT (Decimal) -> integer wei at 18 decimals, floored to whole cents first.

    BEP20 USDT on BSC has 18 decimals (NOT 6). Business rule rounds payouts down to
    the cent; we then scale by 10**18.
    """
    cents = amount.quantize(_CENT, rounding=ROUND_DOWN)
    return int(cents * _WEI)


def native_to_wei(amount: Decimal, decimals: int = 18) -> int:
    """Native-coin amount (Decimal) -> integer base-units at ``decimals``, floored to
    whole cents first (same business rule as :func:`usdt_to_wei`, generalized for a
    non-token native asset). MNT on Mantle is native with 18 decimals.
    """
    cents = amount.quantize(_CENT, rounding=ROUND_DOWN)
    return int(cents * (Decimal(10) ** decimals))


def exact_18_to_wei(amount: Decimal) -> int:
    """Partner obligations retain sub-cent rewards; both supported chains use 18 decimals."""
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid payout amount.")
    with localcontext() as ctx:
        ctx.prec = 80
        return int((amount * _WEI).to_integral_value(rounding=ROUND_DOWN))


class ChainError(Exception):
    """Broadcast/RPC uncertainty — the caller must NOT blindly re-send (see §4.3)."""


class ChainReader(Protocol):
    """The read-only surface shared by every chain client, token or native."""

    async def get_nonce(self, address: str) -> int: ...
    async def get_tx_status(self, txhash: str) -> str: ...
    async def get_confirmations(self, txhash: str) -> int: ...
    async def find_tx_by_nonce(self, address: str, nonce: int) -> str | None: ...
    async def is_nonce_used(self, address: str, nonce: int) -> bool: ...
    async def funds_shortfall(
        self, *, amount_wei: int, token: bool, gas_limit: int
    ) -> FundsShortfall | None:
        """None when the hot wallet can cover paying out ``amount_wei`` plus gas; a
        ``FundsShortfall`` otherwise. ``token=True`` means the amount is an ERC20 balance
        (USDT) while gas is the native coin (BNB); ``token=False`` means the amount IS the
        native coin (MNT) and covers both the transfer and its gas."""
        ...


class ChainClient(ChainReader, Protocol):
    """A token-transfer chain client — USDT on BSC (see §Task 9 for the native sibling)."""

    async def send_transfer(
        self, *, to: str, amount_wei: int, nonce: int, on_signed: OnSigned | None = None
    ) -> str: ...
    async def quote_gas_usdt(self, gas_limit: int) -> GasQuote | None: ...


class NativeChainClient(ChainReader, Protocol):
    """A native-coin chain client on a DISTINCT EVM chain from ``ChainClient`` — e.g. MNT
    on Mantle. No token contract, no gas quote/deduction: gas for native payouts is paid
    in the native coin and borne by the wallet owner (see PayoutExecutor.process_next).
    """

    async def send_native(
        self, *, to: str, amount_wei: int, nonce: int, on_signed: OnSigned | None = None
    ) -> str: ...


class _Tx:
    __slots__ = ("txhash", "nonce", "status", "confirmations")

    def __init__(self, txhash: str, nonce: int, status: str, confirmations: int) -> None:
        self.txhash = txhash
        self.nonce = nonce
        self.status = status
        self.confirmations = confirmations


class MockChainClient:
    """Deterministic in-memory chain for tests. Programmable per-test.

    address is fixed as the sending wallet ("0xwallet"); txhashes are derived from nonce
    so they're stable without randomness (Math.random/uuid are unavailable in this env
    and tests must be reproducible).
    """

    WALLET = "0xwallet"

    def __init__(
        self,
        *,
        nonce: int = 0,
        fail_send_but_land: bool = False,
        fail_send_no_land: bool = False,
        default_confirmations: int = 15,
        default_status: str = "success",
        gas_quote: GasQuote | None = None,
        token_balance_wei: int | None = None,
        native_balance_wei: int | None = None,
        gas_price_wei: int = 0,
    ) -> None:
        self._nonce = nonce
        self._fail_send_but_land = fail_send_but_land
        self._fail_send_no_land = fail_send_no_land
        self._default_confirmations = default_confirmations
        self._default_status = default_status
        self._gas_quote = gas_quote
        # Wallet balances for the funds pre-flight. None == unlimited (default) so every
        # pre-existing test — which never sets a balance — reads as fully funded.
        self._token_balance_wei = token_balance_wei
        self._native_balance_wei = native_balance_wei
        self._gas_price_wei = gas_price_wei
        self._by_hash: dict[str, _Tx] = {}
        self._by_nonce: dict[int, _Tx] = {}
        self.sent_calls: list[dict[str, object]] = []
        # Separate log for NATIVE sends (e.g. MNT on Mantle) so tests can assert a
        # native send happened distinctly from a token (send_transfer) send.
        self.native_sent_calls: list[dict[str, object]] = []

    @staticmethod
    def _hash_for(nonce: int) -> str:
        return f"0xtx{nonce:064x}"

    async def get_nonce(self, address: str) -> int:
        return self._nonce

    async def _send(
        self,
        calls: list[dict[str, object]],
        *,
        to: str,
        amount_wei: int,
        nonce: int,
        on_signed: OnSigned | None,
    ) -> str:
        # record every attempt BEFORE any raise, so tests can assert broadcast args
        # even for a call that fails without landing.
        calls.append({"to": to, "amount_wei": amount_wei, "nonce": nonce})
        txh = self._hash_for(nonce)
        # Persist the deterministic hash BEFORE "broadcast" (the fail/land logic below),
        # so a crash in the broadcast window still leaves the hash on the payout.
        if on_signed is not None:
            await on_signed(txh)
        tx = _Tx(txh, nonce, self._default_status, self._default_confirmations)
        if self._fail_send_no_land:
            raise ChainError("broadcast failed, nothing landed")
        # record the tx (it "landed") BEFORE optionally raising, so reconcile can find it
        self._by_hash[txh] = tx
        self._by_nonce[nonce] = tx
        if self._fail_send_but_land:
            raise ChainError("RPC timeout after broadcast; status unknown")
        return txh

    async def send_transfer(
        self, *, to: str, amount_wei: int, nonce: int, on_signed: OnSigned | None = None
    ) -> str:
        return await self._send(
            self.sent_calls, to=to, amount_wei=amount_wei, nonce=nonce, on_signed=on_signed
        )

    async def send_native(
        self, *, to: str, amount_wei: int, nonce: int, on_signed: OnSigned | None = None
    ) -> str:
        return await self._send(
            self.native_sent_calls, to=to, amount_wei=amount_wei, nonce=nonce, on_signed=on_signed
        )

    async def get_tx_status(self, txhash: str) -> str:
        tx = self._by_hash.get(txhash)
        return tx.status if tx else "unknown"

    async def get_confirmations(self, txhash: str) -> int:
        tx = self._by_hash.get(txhash)
        return tx.confirmations if tx else 0

    async def find_tx_by_nonce(self, address: str, nonce: int) -> str | None:
        tx = self._by_nonce.get(nonce)
        return tx.txhash if tx else None

    async def is_nonce_used(self, address: str, nonce: int) -> bool:
        # A tx for this nonce landed iff it was recorded (fail_send_no_land never records).
        return nonce in self._by_nonce

    async def funds_shortfall(
        self, *, amount_wei: int, token: bool, gas_limit: int
    ) -> FundsShortfall | None:
        gas_wei = self._gas_price_wei * gas_limit
        if token:
            if self._token_balance_wei is not None and self._token_balance_wei < amount_wei:
                return FundsShortfall("amount", _coins(amount_wei), _coins(self._token_balance_wei))
            if self._native_balance_wei is not None and self._native_balance_wei < gas_wei:
                return FundsShortfall("gas", _coins(gas_wei), _coins(self._native_balance_wei))
            return None
        need = amount_wei + gas_wei
        if self._native_balance_wei is not None and self._native_balance_wei < need:
            return FundsShortfall("amount", _coins(need), _coins(self._native_balance_wei))
        return None

    async def quote_gas_usdt(self, gas_limit: int) -> GasQuote | None:
        return self._gas_quote

    # --- test helpers ---
    def set_confirmations(self, nonce: int, n: int) -> None:
        self._by_nonce[nonce].confirmations = n

    def set_status(self, nonce: int, status: str) -> None:
        self._by_nonce[nonce].status = status
