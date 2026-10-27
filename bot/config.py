"""Application configuration.

Loads settings from the environment (pydantic-settings), validates them, and
derives the set of exchanges that are actually usable — an exchange is
"available" only when every credential it needs is present. Exchange secrets may
be stored encrypted (Fernet); they are decrypted lazily, in memory, and never
written to disk or logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cached_property

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bot.security.secrets import SecretCipher, SecretError


@dataclass(frozen=True, slots=True)
class ExchangeSpec:
    """Static description of an exchange's credential requirements."""

    code: str
    label: str
    requires_passphrase: bool = False
    requires_secret: bool = True

    @property
    def env_prefix(self) -> str:
        return self.code.upper()


# Source of truth for which exchanges exist and what each one needs.
# Order is the user-facing display order (most reliable specs first, per TZ).
EXCHANGE_SPECS: tuple[ExchangeSpec, ...] = (
    ExchangeSpec("gate", "Gate"),
    ExchangeSpec("kucoin", "KuCoin", requires_passphrase=True),
    ExchangeSpec("mexc", "MEXC"),
    ExchangeSpec("bitget", "Bitget", requires_passphrase=True),
    ExchangeSpec("bitmart", "BitMart", requires_secret=False),
    ExchangeSpec("okx", "OKX", requires_passphrase=True),
    ExchangeSpec("bybit", "Bybit"),
    ExchangeSpec("weex", "WEEX", requires_passphrase=True),
    ExchangeSpec("toobit", "Toobit"),  # Binance-style; key+secret, no passphrase.
    ExchangeSpec("binance", "Binance"),  # Api-Agent apiReferral; key+secret, no passphrase.
)

SPEC_BY_CODE: dict[str, ExchangeSpec] = {spec.code: spec for spec in EXCHANGE_SPECS}


@dataclass(frozen=True, slots=True)
class ExchangeCredentials:
    """Decrypted, ready-to-use credentials for a single exchange."""

    spec: ExchangeSpec
    api_key: SecretStr
    api_secret: SecretStr
    passphrase: SecretStr | None = None


class Settings(BaseSettings):
    """Environment-backed configuration. Instantiated once via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Telegram (roles) ----
    bot_token: SecretStr = Field(..., alias="BOT_TOKEN")
    # DEPRECATED: the bot is now open to everyone, so this no longer gates access.
    # Kept optional for backward compatibility (old .env files still set it); env IDs
    # that matter are ADMIN_TELEGRAM_IDS (roles) and DEVELOPER_TELEGRAM_ID (alerts).
    # Kept as a raw string so pydantic-settings doesn't JSON-decode it; parsed by
    # the `user_telegram_ids` property below.
    user_telegram_ids_raw: str = Field(default="", alias="USER_TELEGRAM_IDS")
    # The developer who receives alerts (and may also use the bot).
    developer_telegram_id: int = Field(..., alias="DEVELOPER_TELEGRAM_ID")
    # Admin subset (Phase 6): extra gate on top of the whitelist, checked by
    # bot.admin.filters.IsAdmin. Kept as a raw string for the same reason as
    # `user_telegram_ids_raw`; parsed by `admin_telegram_ids` below. Empty by
    # default — no one is admin until explicitly configured.
    admin_telegram_ids_raw: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")

    # ---- Secret encryption ----
    # When set, exchange credentials are treated as Fernet tokens and decrypted.
    master_key: SecretStr | None = Field(default=None, alias="MASTER_KEY")

    # ---- Admin-initiated affiliate credential rotation ----
    credential_update_enabled: bool = Field(default=False, alias="CREDENTIAL_UPDATE_ENABLED")
    credential_updater_socket: str = Field(
        default="/run/rebate-bot/credential-updater.sock",
        alias="CREDENTIAL_UPDATER_SOCKET",
    )
    credential_updater_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=30,
        alias="CREDENTIAL_UPDATER_TIMEOUT_SECONDS",
    )

    # ---- Networking ----
    http_connect_timeout: float = Field(default=10.0, alias="HTTP_CONNECT_TIMEOUT")
    http_read_timeout: float = Field(default=30.0, alias="HTTP_READ_TIMEOUT")
    http_max_retries: int = Field(default=3, alias="HTTP_MAX_RETRIES")

    # ---- Anti-flood ----
    rate_limit_seconds: float = Field(default=2.0, alias="RATE_LIMIT_SECONDS")

    # ---- Demo ----
    # When true, every exchange is served by a stub adapter returning fake data,
    # so the full UX can be exercised without real API keys. Never enable in prod.
    demo_mode: bool = Field(default=False, alias="DEMO_MODE")

    # ---- Accrual ----
    # Global program start: rebate is never counted before this date, for anyone.
    rebate_epoch: date = Field(default=date(2026, 7, 10), alias="REBATE_EPOCH")
    # How often the accrual worker runs a cycle. Default daily (86400s); set lower
    # (e.g. 21600 = every 6h, 4x/day) so a newly-bound UID's rebate — and the
    # whole-downline snapshot — refresh sooner than once a day. The cycle is
    # idempotent, so more frequent runs only catch up faster; they never double-count.
    accrual_interval_seconds: float = Field(default=86400.0, alias="ACCRUAL_INTERVAL_SECONDS")
    # Days to wait before freezing a KuCoin day, so its affiliate commission has settled.
    # KuCoin credits partner commission with a delay: a day polled too early returns
    # records with commission still 0, which the worker would freeze permanently (see the
    # 2026-08 backfill of user 6483207550). Deferring the freeze by this many days lets the
    # amounts settle. Applied PER-USER: a user with ANY KuCoin account has their whole
    # recent window deferred — never freeze a day only partially, which would corrupt that
    # day's rank. 0 disables. No money is lost by deferring: availability is still D+1.
    kucoin_settlement_lag_days: int = Field(default=2, alias="KUCOIN_SETTLEMENT_LAG_DAYS")

    # ---- Payout executor ----
    # Payouts over this amount go to `pending_admin` instead of auto-sending (Phase 6 releases).
    auto_limit_usdt: Decimal = Field(default=Decimal("200"), alias="AUTO_LIMIT_USDT")
    # On-chain confirmations required before a `sent` payout is finalized as `paid`.
    confirmations: int = Field(default=15, alias="CONFIRMATIONS")
    # Sending wallet address (public). The private key below is deploy-time only.
    payout_wallet_address: str | None = Field(default=None, alias="PAYOUT_WALLET_ADDRESS")
    # Payout wallet PRIVATE KEY — the single highest-value secret. Set ONLY at deploy via
    # env; SecretStr so it never lands in logs/reprs. Absent => the real ChainClient is not
    # built and the executor falls back to MockChainClient (no on-chain sends).
    payout_private_key: SecretStr | None = Field(default=None, alias="PAYOUT_PRIVATE_KEY")
    # Comma-separated BSC RPC endpoints for the real ChainClient (unused by MockChainClient);
    # kept as a raw string so pydantic-settings doesn't JSON-decode it, mirroring
    # `user_telegram_ids_raw` — parsed by the `bsc_rpc_urls` property below.
    bsc_rpc_urls_raw: str = Field(default="", alias="BSC_RPC_URLS")
    # BEP20 USDT contract address on BSC (deploy-time; unused by MockChainClient).
    usdt_contract: str | None = Field(default=None, alias="USDT_CONTRACT")
    # BSC chain id (56 mainnet, 97 testnet). Deploy-time; unused by MockChainClient.
    bsc_chain_id: int = Field(default=56, alias="BSC_CHAIN_ID")
    # Chainlink BNB/USD price-feed aggregator (BSC mainnet default) used to convert the
    # gas cost to USDT for per-payout gas deduction. Empty/unreachable => no deduction.
    bnb_usd_feed: str = Field(
        default="0x0567F2323251f0Aab15c8dFb1967E4e8A7D42aeE", alias="BNB_USD_FEED"
    )
    # Fail-closed gas ceiling (wei). If gas price exceeds it, the real client refuses to
    # broadcast rather than overpay. None => no cap (dev/test).
    max_gas_price_wei: int | None = Field(default=None, alias="MAX_GAS_PRICE_WEI")

    # ---- Mantle RPC (MNT payouts) ----
    # Mantle RPC endpoint for on-chain MNT payouts. MNT is the native coin on Mantle.
    # Absent => MNT payouts disabled.
    mantle_rpc_url: str | None = Field(default=None, alias="MANTLE_RPC_URL")
    # Mantle chain id (5000 mainnet). Deploy-time; unused when mnt_enabled is false.
    mantle_chain_id: int = Field(default=5000, alias="MANTLE_CHAIN_ID")
    # Fail-closed gas ceiling (wei) for the MANTLE client specifically — kept separate
    # from MAX_GAS_PRICE_WEI (BSC) since Mantle gas prices run orders of magnitude
    # lower; reusing the BSC cap would either wrongly block a legitimate cheap Mantle
    # send (if sized for BSC) or leave Mantle with no ceiling at all (if left unset).
    # Sanity ceiling for Mantle native-MNT gas, NOT the BSC value: Mantle's baseline
    # eth_gasPrice sits right at ~50 gwei, so a 50 gwei cap would reject every send.
    # Default to 2000 gwei — still trivial in USD for a ~21k-gas native transfer
    # (~0.04 MNT) but with real headroom over the baseline + spikes.
    mantle_max_gas_price_wei: int | None = Field(
        default=2_000_000_000_000, alias="MANTLE_MAX_GAS_PRICE_WEI"
    )

    @field_validator("max_gas_price_wei", "mantle_max_gas_price_wei", mode="before")
    @classmethod
    def _blank_gas_cap_is_none(cls, v: object) -> object:
        # `MAX_GAS_PRICE_WEI=` (empty env value) means "no cap" — not int(""), which raises.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @property
    def mnt_enabled(self) -> bool:
        """True if Mantle RPC is configured (MNT payouts enabled)."""
        return self.mantle_rpc_url is not None

    # ---- Exchange credentials (raw; may be plaintext or Fernet tokens) ----
    bitget_api_key: SecretStr | None = Field(default=None, alias="BITGET_API_KEY")
    bitget_api_secret: SecretStr | None = Field(default=None, alias="BITGET_API_SECRET")
    bitget_api_passphrase: SecretStr | None = Field(default=None, alias="BITGET_API_PASSPHRASE")

    bitmart_api_key: SecretStr | None = Field(default=None, alias="BITMART_API_KEY")
    bitmart_api_secret: SecretStr | None = Field(default=None, alias="BITMART_API_SECRET")

    bybit_api_key: SecretStr | None = Field(default=None, alias="BYBIT_API_KEY")
    bybit_api_secret: SecretStr | None = Field(default=None, alias="BYBIT_API_SECRET")

    weex_api_key: SecretStr | None = Field(default=None, alias="WEEX_API_KEY")
    weex_api_secret: SecretStr | None = Field(default=None, alias="WEEX_API_SECRET")
    weex_api_passphrase: SecretStr | None = Field(default=None, alias="WEEX_API_PASSPHRASE")

    gate_api_key: SecretStr | None = Field(default=None, alias="GATE_API_KEY")
    gate_api_secret: SecretStr | None = Field(default=None, alias="GATE_API_SECRET")

    okx_api_key: SecretStr | None = Field(default=None, alias="OKX_API_KEY")
    okx_api_secret: SecretStr | None = Field(default=None, alias="OKX_API_SECRET")
    okx_api_passphrase: SecretStr | None = Field(default=None, alias="OKX_API_PASSPHRASE")

    kucoin_api_key: SecretStr | None = Field(default=None, alias="KUCOIN_API_KEY")
    kucoin_api_secret: SecretStr | None = Field(default=None, alias="KUCOIN_API_SECRET")
    kucoin_api_passphrase: SecretStr | None = Field(default=None, alias="KUCOIN_API_PASSPHRASE")

    mexc_api_key: SecretStr | None = Field(default=None, alias="MEXC_API_KEY")
    mexc_api_secret: SecretStr | None = Field(default=None, alias="MEXC_API_SECRET")

    toobit_api_key: SecretStr | None = Field(default=None, alias="TOOBIT_API_KEY")
    toobit_api_secret: SecretStr | None = Field(default=None, alias="TOOBIT_API_SECRET")

    binance_api_key: SecretStr | None = Field(default=None, alias="BINANCE_API_KEY")
    # Accept either the project-convention name or the BINANCE_SECRET_KEY the owner used.
    binance_api_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("BINANCE_API_SECRET", "BINANCE_SECRET_KEY"),
    )

    @cached_property
    def user_telegram_ids(self) -> frozenset[int]:
        """Owner IDs parsed from the comma-separated USER_TELEGRAM_IDS value."""
        try:
            return frozenset(
                int(part) for part in self.user_telegram_ids_raw.split(",") if part.strip()
            )
        except ValueError as exc:
            raise ValueError(
                "USER_TELEGRAM_IDS must be comma-separated integers, e.g. '123,456'."
            ) from exc

    @cached_property
    def admin_telegram_ids(self) -> frozenset[int]:
        """Admin IDs parsed from the comma-separated ADMIN_TELEGRAM_IDS value."""
        try:
            return frozenset(
                int(part) for part in self.admin_telegram_ids_raw.split(",") if part.strip()
            )
        except ValueError as exc:
            raise ValueError(
                "ADMIN_TELEGRAM_IDS must be comma-separated integers, e.g. '123,456'."
            ) from exc

    @cached_property
    def bsc_rpc_urls(self) -> list[str]:
        """BSC RPC endpoints parsed from the comma-separated BSC_RPC_URLS value."""
        return [part.strip() for part in self.bsc_rpc_urls_raw.split(",") if part.strip()]

    @cached_property
    def allowed_telegram_ids(self) -> frozenset[int]:
        """DEPRECATED (access is now open): the legacy USER_TELEGRAM_IDS ∪ developer set.

        No longer used to gate the bot — access control moved to the per-user
        ``users.status`` block list (see ``BlockGuardMiddleware``). Retained only so
        older callers/tests keep resolving; safe to ignore for access decisions."""
        return self.user_telegram_ids | {self.developer_telegram_id}

    @cached_property
    def _cipher(self) -> SecretCipher | None:
        # A blank MASTER_KEY (env var present but empty) means "no encryption", not
        # SecretCipher("") — which would raise. Only build a cipher for a real key.
        master = self.master_key
        return (
            SecretCipher(master.get_secret_value())
            if master and master.get_secret_value()
            else None
        )

    def _reveal(self, raw: SecretStr | None) -> SecretStr | None:
        """Decrypt a stored credential if it's a Fernet token, else pass it through.

        Mixed-mode is tolerated: with MASTER_KEY set, values that ARE Fernet tokens are
        decrypted; values that are not (plaintext) fall through unchanged. This decouples
        the wallet cipher (which needs MASTER_KEY) from exchange secrets (which may stay
        plaintext), so enabling MASTER_KEY never breaks plaintext exchange keys."""
        if raw is None:
            return None
        if self._cipher is None:
            return raw
        try:
            return SecretStr(self._cipher.decrypt(raw.get_secret_value()))
        except SecretError:
            return raw  # not a Fernet token -> treat as plaintext

    def _build_credentials(self, spec: ExchangeSpec) -> ExchangeCredentials | None:
        """Return ready credentials for ``spec``, or ``None`` if any are missing."""
        api_key = self._reveal(getattr(self, f"{spec.code}_api_key"))
        api_secret = self._reveal(getattr(self, f"{spec.code}_api_secret"))
        passphrase = (
            self._reveal(getattr(self, f"{spec.code}_api_passphrase"))
            if spec.requires_passphrase
            else None
        )

        if api_key is None:
            return None
        if spec.requires_secret:
            if api_secret is None:
                return None
        else:
            api_secret = api_secret or SecretStr("")
        if spec.requires_passphrase and passphrase is None:
            return None

        return ExchangeCredentials(spec, api_key, api_secret, passphrase)

    @cached_property
    def available_exchanges(self) -> dict[str, ExchangeCredentials]:
        """Exchanges with a full credential set, keyed by code, in display order."""
        result: dict[str, ExchangeCredentials] = {}
        for spec in EXCHANGE_SPECS:
            creds = self._build_credentials(spec)
            if creds is not None:
                result[spec.code] = creds
        return result


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton (lazy, so imports stay side-effect-free)."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]  # values come from env
    return _settings
