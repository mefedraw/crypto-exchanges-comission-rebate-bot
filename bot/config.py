"""Application configuration.

Loads settings from the environment (pydantic-settings), validates them, and
derives the set of exchanges that are actually usable — an exchange is
"available" only when every credential it needs is present. Exchange secrets may
be stored encrypted (Fernet); they are decrypted lazily, in memory, and never
written to disk or logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from bot.security.secrets import SecretCipher


@dataclass(frozen=True, slots=True)
class ExchangeSpec:
    """Static description of an exchange's credential requirements."""

    code: str
    label: str
    requires_passphrase: bool = False

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
    ExchangeSpec("okx", "OKX", requires_passphrase=True),
    ExchangeSpec("bybit", "Bybit"),
    ExchangeSpec("weex", "WEEX", requires_passphrase=True),
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
    # The owner who uses the bot to calculate commission.
    user_telegram_id: int = Field(..., alias="USER_TELEGRAM_ID")
    # The developer who receives alerts (and may also use the bot).
    developer_telegram_id: int = Field(..., alias="DEVELOPER_TELEGRAM_ID")

    # ---- Secret encryption ----
    # When set, exchange credentials are treated as Fernet tokens and decrypted.
    master_key: SecretStr | None = Field(default=None, alias="MASTER_KEY")

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

    # ---- Exchange credentials (raw; may be plaintext or Fernet tokens) ----
    bitget_api_key: SecretStr | None = Field(default=None, alias="BITGET_API_KEY")
    bitget_api_secret: SecretStr | None = Field(default=None, alias="BITGET_API_SECRET")
    bitget_api_passphrase: SecretStr | None = Field(default=None, alias="BITGET_API_PASSPHRASE")

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

    @cached_property
    def allowed_telegram_ids(self) -> frozenset[int]:
        """Whitelist = the two role IDs (deduped). Everyone else is ignored."""
        return frozenset({self.user_telegram_id, self.developer_telegram_id})

    @cached_property
    def _cipher(self) -> SecretCipher | None:
        master = self.master_key
        return SecretCipher(master.get_secret_value()) if master else None

    def _reveal(self, raw: SecretStr | None) -> SecretStr | None:
        """Decrypt a stored credential if encryption is enabled, else pass through."""
        if raw is None:
            return None
        if self._cipher is None:
            return raw
        return SecretStr(self._cipher.decrypt(raw.get_secret_value()))

    def _build_credentials(self, spec: ExchangeSpec) -> ExchangeCredentials | None:
        """Return ready credentials for ``spec``, or ``None`` if any are missing."""
        api_key = self._reveal(getattr(self, f"{spec.code}_api_key"))
        api_secret = self._reveal(getattr(self, f"{spec.code}_api_secret"))
        passphrase = (
            self._reveal(getattr(self, f"{spec.code}_api_passphrase"))
            if spec.requires_passphrase
            else None
        )

        if api_key is None or api_secret is None:
            return None
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
