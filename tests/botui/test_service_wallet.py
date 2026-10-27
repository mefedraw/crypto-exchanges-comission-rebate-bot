# tests/botui/test_service_wallet.py
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import repository as repo
from bot.botui import service
from bot.security.secrets import SecretCipher

pytestmark = pytest.mark.asyncio(loop_scope="session")

GOOD = "0x" + "ab" * 20


def test_validate_address_accepts_and_lowercases():
    assert service.validate_address("  0x" + "AB" * 20 + " ") == "0x" + "ab" * 20


@pytest.mark.parametrize("bad", ["0x123", "nothex", "0X" + "z" * 40, "0x" + "ab" * 21])
def test_validate_address_rejects(bad):
    with pytest.raises(service.AddressError):
        service.validate_address(bad)


def test_mask_address():
    assert service.mask_address(GOOD) == "0xabab…abab"


async def test_set_wallet_encrypts_and_persists(db_session: AsyncSession):
    cipher = SecretCipher(Fernet.generate_key().decode())
    await repo.upsert_user(db_session, 910)
    masked = await service.set_wallet(db_session, cipher, 910, GOOD)
    assert masked == "0xabab…abab"
    w = await repo.get_active_wallet(db_session, 910)
    assert w is not None
    assert w.address_encrypted != GOOD.encode()  # stored encrypted, not plaintext
    assert cipher.decrypt(w.address_encrypted.decode()) == GOOD  # round-trips
