from bot.config import Settings
from tests.conftest import make_settings


def test_mnt_disabled_by_default() -> None:
    s = make_settings()
    assert s.mnt_enabled is False


def test_mnt_enabled_when_mantle_rpc_set() -> None:
    s = make_settings(mantle_rpc_url="https://rpc.mantle.xyz", mantle_chain_id=5000)
    assert s.mnt_enabled is True and s.mantle_chain_id == 5000
