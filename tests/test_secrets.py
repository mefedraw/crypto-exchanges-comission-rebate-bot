from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from bot.security.secrets import SecretCipher, SecretError


def test_encrypt_decrypt_round_trip():
    cipher = SecretCipher(Fernet.generate_key().decode())
    token = cipher.encrypt("super-secret-key")
    assert token != "super-secret-key"
    assert cipher.decrypt(token) == "super-secret-key"


def test_invalid_master_key():
    with pytest.raises(SecretError):
        SecretCipher("not-a-valid-fernet-key")


def test_decrypt_with_wrong_key_fails():
    a = SecretCipher(Fernet.generate_key().decode())
    b = SecretCipher(Fernet.generate_key().decode())
    token = a.encrypt("x")
    with pytest.raises(SecretError):
        b.decrypt(token)
