"""Read payout-wallet balances without loading a signing key."""

from __future__ import annotations

from decimal import Decimal

from web3 import AsyncHTTPProvider, AsyncWeb3

from bot.config import Settings

_BALANCE_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]
_WEI = Decimal(10) ** 18


class WalletBalanceReader:
    def __init__(
        self, *, rpc_url: str, address: str, chain_id: int, token: str | None = None
    ) -> None:
        self._provider = AsyncHTTPProvider(rpc_url, request_kwargs={"timeout": 10})
        self._w3 = AsyncWeb3(self._provider)
        self.address = self._w3.to_checksum_address(address)
        self._chain_id = chain_id
        self._token = self._w3.to_checksum_address(token) if token else None

    async def _check_chain(self) -> None:
        if await self._w3.eth.chain_id != self._chain_id:
            raise ValueError("Balance RPC returned an unexpected chain")

    async def native_balance(self) -> Decimal:
        await self._check_chain()
        return Decimal(await self._w3.eth.get_balance(self.address)) / _WEI

    async def usdt_balance(self) -> Decimal | None:
        if self._token is None:
            return None
        await self._check_chain()
        contract = self._w3.eth.contract(address=self._token, abi=_BALANCE_ABI)
        return Decimal(await contract.functions.balanceOf(self.address).call()) / _WEI

    async def aclose(self) -> None:
        await self._provider.disconnect()


def build_bsc_balance_reader(settings: Settings) -> WalletBalanceReader | None:
    if (
        not settings.payout_wallet_address
        or not settings.bsc_rpc_urls
        or not settings.usdt_contract
    ):
        return None
    return WalletBalanceReader(
        rpc_url=settings.bsc_rpc_urls[0],
        address=settings.payout_wallet_address,
        chain_id=settings.bsc_chain_id,
        token=settings.usdt_contract,
    )


def build_mantle_balance_reader(settings: Settings) -> WalletBalanceReader | None:
    if not settings.payout_wallet_address or not settings.mantle_rpc_url:
        return None
    return WalletBalanceReader(
        rpc_url=settings.mantle_rpc_url,
        address=settings.payout_wallet_address,
        chain_id=settings.mantle_chain_id,
    )
