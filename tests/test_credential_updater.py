from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from bot.security.credential_updater import (
    CredentialUpdaterServer,
    EnvFileStore,
    UpdaterConfig,
    UpdateRejected,
    UpdateRequest,
    validate_request,
)


def _token() -> str:
    cipher = Fernet(Fernet.generate_key())
    return cipher.encrypt(b"secret").decode("ascii")


def _gate_payload() -> dict[str, object]:
    return {
        "version": 1,
        "request_id": str(uuid.uuid4()),
        "actor_id": 111,
        "exchange": "gate",
        "values": {"GATE_API_KEY": _token(), "GATE_API_SECRET": _token()},
    }


def test_validate_request_accepts_only_exact_affiliate_field_set() -> None:
    request = validate_request(_gate_payload())
    assert request.exchange == "gate"
    assert set(request.values) == {"GATE_API_KEY", "GATE_API_SECRET"}

    payload = _gate_payload()
    values = payload["values"]
    assert isinstance(values, dict)
    values["WITHDRAWAL_MEXC_API_KEY"] = _token()
    with pytest.raises(UpdateRejected, match="not allowlisted"):
        validate_request(payload)

    payload = _gate_payload()
    payload["values"] = {"GATE_API_KEY": _token()}
    with pytest.raises(UpdateRejected, match="incomplete"):
        validate_request(payload)


def test_validate_request_rejects_plaintext_and_unknown_exchange() -> None:
    payload = _gate_payload()
    payload["values"] = {
        "GATE_API_KEY": "plaintext-key",
        "GATE_API_SECRET": "plaintext-secret",
    }
    with pytest.raises(UpdateRejected, match="malformed"):
        validate_request(payload)

    payload = _gate_payload()
    payload["exchange"] = "unknown"
    with pytest.raises(UpdateRejected, match="allowlisted"):
        validate_request(payload)


def test_env_store_is_atomic_restrictive_and_restorable(tmp_path) -> None:
    directory = tmp_path.resolve() / "credential-store"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / "exchange-keys.env"
    store = EnvFileStore(path, require_root_owner=False)
    gate_values = {"GATE_API_KEY": _token(), "GATE_API_SECRET": _token()}
    kucoin_values = {
        "KUCOIN_API_KEY": _token(),
        "KUCOIN_API_SECRET": _token(),
        "KUCOIN_API_PASSPHRASE": _token(),
    }

    absent = store.apply(gate_values)
    gate_snapshot = path.read_bytes()
    assert absent.existed is False
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b"plaintext" not in gate_snapshot

    previous = store.apply(kucoin_values)
    combined = path.read_text(encoding="ascii")
    assert previous.content == gate_snapshot
    assert "GATE_API_KEY=" in combined
    assert "KUCOIN_API_KEY=" in combined

    store.restore(previous)
    assert path.read_bytes() == gate_snapshot
    store.restore(absent)
    assert not path.exists()


def test_env_store_rejects_unmanaged_content(tmp_path) -> None:
    directory = tmp_path.resolve() / "credential-store"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / "exchange-keys.env"
    path.write_text("MASTER_KEY=must-not-be-managed-here\n", encoding="ascii")
    os.chmod(path, 0o600)
    store = EnvFileStore(path, require_root_owner=False)

    with pytest.raises(UpdateRejected, match="invalid field"):
        store.apply({"GATE_API_KEY": _token(), "GATE_API_SECRET": _token()})


def test_updater_config_rejects_non_allowlisted_restart_service(monkeypatch) -> None:
    monkeypatch.setenv("CREDENTIAL_COMPOSE_PROJECT_NAME", "rebate-bot")
    monkeypatch.setenv("CREDENTIAL_RESTART_SERVICES", "bot,db")

    with pytest.raises(RuntimeError, match="RESTART_SERVICES"):
        UpdaterConfig.from_env()


async def test_restart_failure_restores_previous_file(monkeypatch, tmp_path) -> None:
    directory = tmp_path.resolve() / "credential-store"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    env_file = directory / "exchange-keys.env"
    store = EnvFileStore(env_file, require_root_owner=False)
    old_values = {"GATE_API_KEY": _token(), "GATE_API_SECRET": _token()}
    new_values = {"GATE_API_KEY": _token(), "GATE_API_SECRET": _token()}
    store.apply(old_values)
    old_content = env_file.read_bytes()
    previous = store.apply(new_values)
    request = UpdateRequest(str(uuid.uuid4()), 111, "gate", new_values)
    config = UpdaterConfig(
        socket_path=Path(tmp_path.resolve() / "updater.sock"),
        env_file=env_file,
        compose_project_dir=tmp_path.resolve(),
        compose_file=Path(tmp_path.resolve() / "docker-compose.yml"),
        compose_project_name="rebate-bot",
        restart_services=("bot",),
        allowed_peer_uid=10001,
        socket_gid=10001,
        restart_delay_seconds=0,
        health_settle_seconds=0,
    )
    server = CredentialUpdaterServer(config)
    server._store = store
    server._seen_request_ids.add(request.request_id)
    attempts = 0

    def recreate() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("compose failed")

    monkeypatch.setattr(server, "_run_compose_recreate", recreate)

    await server._restart_or_rollback(previous, request)

    assert attempts == 2
    assert env_file.read_bytes() == old_content
    assert request.request_id not in server._seen_request_ids
