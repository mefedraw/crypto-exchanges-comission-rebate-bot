from __future__ import annotations

from datetime import datetime

import pytest
from cryptography.fernet import Fernet

from bot.config import SPEC_BY_CODE
from bot.exchanges.base import CommissionResult, ExchangeAuthError
from bot.security.credential_rotation import (
    CredentialInputError,
    CredentialRotationService,
    CredentialValidationError,
    parse_credential_message,
)
from bot.security.secrets import SecretCipher
from tests.conftest import make_settings


class _FakeUpdater:
    def __init__(self) -> None:
        self.exchange: str | None = None
        self.values: dict[str, str] | None = None
        self.actor_id: int | None = None

    async def apply(
        self,
        exchange: str,
        encrypted_values: dict[str, str],
        *,
        actor_id: int,
    ) -> str:
        self.exchange = exchange
        self.values = encrypted_values
        self.actor_id = actor_id
        return "request-id"


class _FakeAdapter:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, datetime, datetime]] = []
        self.closed = False

    async def get_commission(
        self,
        uid: str,
        date_from: datetime,
        date_to: datetime,
    ) -> CommissionResult:
        self.calls.append((uid, date_from, date_to))
        if self.error is not None:
            raise self.error
        return CommissionResult("Gate", uid, date_from, date_to)

    async def aclose(self) -> None:
        self.closed = True


def test_parse_complete_bundle_and_reject_whitespace() -> None:
    bundle = parse_credential_message(SPEC_BY_CODE["kucoin"], "key\nsecret\nphrase")
    assert bundle.api_key.get_secret_value() == "key"
    assert bundle.api_secret.get_secret_value() == "secret"
    assert bundle.passphrase is not None
    assert bundle.passphrase.get_secret_value() == "phrase"

    with pytest.raises(CredentialInputError):
        parse_credential_message(SPEC_BY_CODE["gate"], " key\nsecret")
    with pytest.raises(CredentialInputError):
        parse_credential_message(SPEC_BY_CODE["gate"], "key only\nsecret")
    with pytest.raises(CredentialInputError):
        parse_credential_message(SPEC_BY_CODE["gate"], "key")


def test_bitmart_accepts_only_key_and_encrypts_complete_env() -> None:
    master_key = Fernet.generate_key().decode()
    cipher = SecretCipher(master_key)
    bundle = parse_credential_message(SPEC_BY_CODE["bitmart"], "key")

    encrypted = bundle.encrypted_env(cipher)

    assert set(encrypted) == {"BITMART_API_KEY", "BITMART_API_SECRET"}
    assert cipher.decrypt(encrypted["BITMART_API_KEY"]) == "key"
    assert cipher.decrypt(encrypted["BITMART_API_SECRET"]) == ""
    assert "key" not in encrypted.values()


async def test_rotate_validates_before_persisting_encrypted_values(monkeypatch) -> None:
    master_key = Fernet.generate_key().decode()
    settings = make_settings(MASTER_KEY=master_key)
    updater = _FakeUpdater()
    adapter = _FakeAdapter()
    monkeypatch.setattr(
        "bot.security.credential_rotation.build_adapter_with_credentials",
        lambda settings, credentials: adapter,
    )
    bundle = parse_credential_message(SPEC_BY_CODE["gate"], "new-key\nnew-secret")

    receipt = await CredentialRotationService(settings, updater).rotate(
        bundle,
        verification_uid="12345678",
        actor_id=111,
    )

    assert adapter.calls and adapter.calls[0][0] == "12345678"
    assert adapter.closed is True
    assert updater.exchange == "gate"
    assert updater.actor_id == 111
    assert updater.values is not None
    cipher = SecretCipher(master_key)
    assert cipher.decrypt(updater.values["GATE_API_KEY"]) == "new-key"
    assert cipher.decrypt(updater.values["GATE_API_SECRET"]) == "new-secret"
    assert receipt.masked_api_key == "****-key"


async def test_failed_validation_never_calls_updater(monkeypatch) -> None:
    settings = make_settings(MASTER_KEY=Fernet.generate_key().decode())
    updater = _FakeUpdater()
    adapter = _FakeAdapter(error=ExchangeAuthError("bad credentials"))
    monkeypatch.setattr(
        "bot.security.credential_rotation.build_adapter_with_credentials",
        lambda settings, credentials: adapter,
    )
    bundle = parse_credential_message(SPEC_BY_CODE["gate"], "new-key\nnew-secret")

    with pytest.raises(CredentialValidationError):
        await CredentialRotationService(settings, updater).rotate(
            bundle,
            verification_uid="12345678",
            actor_id=111,
        )

    assert updater.values is None
    assert adapter.closed is True
