from __future__ import annotations

import base64

import pytest

from bot.exchanges.signing import hmac_base64, hmac_hex, sha512_hex

# Well-known HMAC-SHA256 test vector (key="key", msg=pangram).
_KEY = "key"
_MSG = "The quick brown fox jumps over the lazy dog"
_EXPECTED_HEX = "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"


def test_hmac_hex_known_vector():
    assert hmac_hex(_KEY, _MSG) == _EXPECTED_HEX


def test_hmac_base64_matches_hex_digest():
    expected_b64 = base64.b64encode(bytes.fromhex(_EXPECTED_HEX)).decode("ascii")
    assert hmac_base64(_KEY, _MSG) == expected_b64


def test_sha512_hex_empty_string():
    assert sha512_hex("") == (
        "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce"
        "47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e"
    )


def test_unsupported_algorithm_rejected():
    with pytest.raises(ValueError):
        hmac_hex(_KEY, _MSG, algorithm="md5")
