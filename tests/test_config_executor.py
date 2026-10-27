from __future__ import annotations

from decimal import Decimal

from bot.config import Settings


def _base_env(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("USER_TELEGRAM_IDS", "1")
    monkeypatch.setenv("DEVELOPER_TELEGRAM_ID", "2")


def test_auto_limit_usdt_defaults(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("AUTO_LIMIT_USDT", raising=False)
    assert Settings().auto_limit_usdt == Decimal("200")  # type: ignore[call-arg]


def test_auto_limit_usdt_parses_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("AUTO_LIMIT_USDT", "150.50")
    assert Settings().auto_limit_usdt == Decimal("150.50")  # type: ignore[call-arg]


def test_confirmations_defaults_to_15(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("CONFIRMATIONS", raising=False)
    assert Settings().confirmations == 15  # type: ignore[call-arg]


def test_confirmations_parses_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("CONFIRMATIONS", "20")
    assert Settings().confirmations == 20  # type: ignore[call-arg]


def test_bsc_rpc_urls_splits_and_strips_blanks(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("BSC_RPC_URLS", "a,b, ,c")
    assert Settings().bsc_rpc_urls == ["a", "b", "c"]  # type: ignore[call-arg]


def test_bsc_rpc_urls_defaults_empty(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("BSC_RPC_URLS", raising=False)
    assert Settings().bsc_rpc_urls == []  # type: ignore[call-arg]


def test_payout_wallet_address_and_usdt_contract_default_none(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("PAYOUT_WALLET_ADDRESS", raising=False)
    monkeypatch.delenv("USDT_CONTRACT", raising=False)
    # _env_file=None: assert the code defaults, not whatever the developer's local .env holds.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.payout_wallet_address is None
    assert settings.usdt_contract is None


def test_blank_max_gas_price_wei_is_none(monkeypatch):
    # `MAX_GAS_PRICE_WEI=` (empty env value) means "no cap" — must NOT crash on int("").
    _base_env(monkeypatch)
    monkeypatch.setenv("MAX_GAS_PRICE_WEI", "")
    assert Settings().max_gas_price_wei is None  # type: ignore[call-arg]


def test_max_gas_price_wei_parses_when_set(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("MAX_GAS_PRICE_WEI", "5000000000")
    assert Settings().max_gas_price_wei == 5_000_000_000  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# FINDING 3: MANTLE_MAX_GAS_PRICE_WEI — a chain-appropriate cap for the Mantle
# client, distinct from BSC's MAX_GAS_PRICE_WEI (Mantle gas prices are orders of
# magnitude lower; reusing the BSC cap risks wrongly blocking a legitimate cheap
# Mantle send whenever an operator sets a BSC-scale MAX_GAS_PRICE_WEI).
# ---------------------------------------------------------------------------


def test_mantle_max_gas_price_wei_has_sensible_nonnone_default(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("MANTLE_MAX_GAS_PRICE_WEI", raising=False)
    # Unlike MAX_GAS_PRICE_WEI (defaults to None = no cap), Mantle gets SOME sanity
    # ceiling out of the box so a deploy that only sets MAX_GAS_PRICE_WEI for BSC
    # doesn't leave the Mantle client with zero gas-price protection.
    assert Settings().mantle_max_gas_price_wei is not None  # type: ignore[call-arg]


def test_blank_mantle_max_gas_price_wei_is_none(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("MANTLE_MAX_GAS_PRICE_WEI", "")
    assert Settings().mantle_max_gas_price_wei is None  # type: ignore[call-arg]


def test_mantle_max_gas_price_wei_parses_when_set(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("MANTLE_MAX_GAS_PRICE_WEI", "50000000000")
    assert Settings().mantle_max_gas_price_wei == 50_000_000_000  # type: ignore[call-arg]


def test_mantle_max_gas_price_wei_independent_of_bsc_cap(monkeypatch):
    # Setting a tiny BSC cap must not affect the (separately configured) Mantle cap.
    _base_env(monkeypatch)
    monkeypatch.setenv("MAX_GAS_PRICE_WEI", "1")
    monkeypatch.setenv("MANTLE_MAX_GAS_PRICE_WEI", "50000000000")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.max_gas_price_wei == 1
    assert settings.mantle_max_gas_price_wei == 50_000_000_000
