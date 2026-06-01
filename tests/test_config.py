from __future__ import annotations

from tests.conftest import make_settings


def test_allowed_ids_from_roles():
    s = make_settings(USER_TELEGRAM_IDS="1", DEVELOPER_TELEGRAM_ID="2")
    assert s.allowed_telegram_ids == frozenset({1, 2})


def test_multiple_users_plus_developer():
    s = make_settings(USER_TELEGRAM_IDS="1, 2 ,3", DEVELOPER_TELEGRAM_ID="9")
    assert s.user_telegram_ids == frozenset({1, 2, 3})
    assert s.allowed_telegram_ids == frozenset({1, 2, 3, 9})


def test_allowed_ids_dedupe_when_roles_equal():
    s = make_settings(USER_TELEGRAM_IDS="5", DEVELOPER_TELEGRAM_ID="5")
    assert s.allowed_telegram_ids == frozenset({5})


def test_available_exchanges_requires_full_credentials():
    s = make_settings(
        GATE_API_KEY="k",
        GATE_API_SECRET="s",
        # Bitget missing passphrase => not available.
        BITGET_API_KEY="k",
        BITGET_API_SECRET="s",
    )
    available = s.available_exchanges
    assert "gate" in available
    assert "bitget" not in available


def test_available_exchanges_with_passphrase():
    s = make_settings(
        KUCOIN_API_KEY="k",
        KUCOIN_API_SECRET="s",
        KUCOIN_API_PASSPHRASE="p",
    )
    assert "kucoin" in s.available_exchanges
    creds = s.available_exchanges["kucoin"]
    assert creds.api_key.get_secret_value() == "k"
    assert creds.passphrase is not None


def test_encrypted_credentials_are_decrypted():
    from cryptography.fernet import Fernet

    from bot.security.secrets import SecretCipher

    key = Fernet.generate_key().decode()
    cipher = SecretCipher(key)
    s = make_settings(
        MASTER_KEY=key,
        GATE_API_KEY=cipher.encrypt("real-key"),
        GATE_API_SECRET=cipher.encrypt("real-secret"),
    )
    creds = s.available_exchanges["gate"]
    assert creds.api_key.get_secret_value() == "real-key"
    assert creds.api_secret.get_secret_value() == "real-secret"


def test_demo_mode_flag():
    assert make_settings(DEMO_MODE="true").demo_mode is True
    assert make_settings().demo_mode is False
