from __future__ import annotations

from cryptography.fernet import Fernet

from bot.config import Settings
from bot.security.secrets import SecretCipher


def _base_env(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("USER_TELEGRAM_IDS", "1")
    monkeypatch.setenv("DEVELOPER_TELEGRAM_ID", "2")


def test_blank_master_key_means_no_cipher(monkeypatch):
    # MASTER_KEY present but empty must NOT build SecretCipher("") (which raises).
    _base_env(monkeypatch)
    monkeypatch.setenv("MASTER_KEY", "")
    monkeypatch.setenv("GATE_API_KEY", "plainkey")
    monkeypatch.setenv("GATE_API_SECRET", "plainsecret")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.available_exchanges["gate"].api_secret.get_secret_value() == "plainsecret"


def test_mixed_plaintext_and_encrypted_secrets(monkeypatch):
    # With MASTER_KEY set: Fernet tokens are decrypted; plaintext values pass through.
    key = Fernet.generate_key().decode()
    cipher = SecretCipher(key)
    _base_env(monkeypatch)
    monkeypatch.setenv("MASTER_KEY", key)
    # gate = plaintext creds
    monkeypatch.setenv("GATE_API_KEY", "plainkey")
    monkeypatch.setenv("GATE_API_SECRET", "plainsecret")
    # bybit = encrypted creds
    monkeypatch.setenv("BYBIT_API_KEY", cipher.encrypt("bkey"))
    monkeypatch.setenv("BYBIT_API_SECRET", cipher.encrypt("bsecret"))
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    creds = s.available_exchanges
    assert creds["gate"].api_secret.get_secret_value() == "plainsecret"  # passthrough
    assert creds["bybit"].api_secret.get_secret_value() == "bsecret"  # decrypted
