"""Isolated root helper for atomically updating encrypted exchange credentials.

The helper has a deliberately tiny Unix-socket protocol. It accepts only a
complete allowlisted credential set, writes a dedicated env file, and recreates
a fixed list of Compose services. It never receives plaintext API credentials.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import socket
import stat
import struct
import subprocess
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

_MAX_REQUEST_BYTES = 32 * 1024
_REQUEST_TIMEOUT_SECONDS = 10
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{80,2048}={0,2}\Z")
_SERVICE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}\Z")
_ALLOWED_RESTART_SERVICES = frozenset({"bot", "accrual"})

_FIELDS_BY_EXCHANGE: dict[str, frozenset[str]] = {
    "gate": frozenset({"GATE_API_KEY", "GATE_API_SECRET"}),
    "kucoin": frozenset({"KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_API_PASSPHRASE"}),
    "mexc": frozenset({"MEXC_API_KEY", "MEXC_API_SECRET"}),
    "bitget": frozenset({"BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE"}),
    "bitmart": frozenset({"BITMART_API_KEY", "BITMART_API_SECRET"}),
    "okx": frozenset({"OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"}),
    "bybit": frozenset({"BYBIT_API_KEY", "BYBIT_API_SECRET"}),
    "weex": frozenset({"WEEX_API_KEY", "WEEX_API_SECRET", "WEEX_API_PASSPHRASE"}),
    "toobit": frozenset({"TOOBIT_API_KEY", "TOOBIT_API_SECRET"}),
    "binance": frozenset({"BINANCE_API_KEY", "BINANCE_API_SECRET"}),
}
_ALL_FIELDS = frozenset().union(*_FIELDS_BY_EXCHANGE.values())
_FIELD_ORDER = tuple(sorted(_ALL_FIELDS))

logger = logging.getLogger("credential-updater")


class UpdateRejected(ValueError):
    """A request or target file violates the updater's strict policy."""


def _chown(path: Path, uid: int, gid: int) -> None:
    chown = getattr(os, "chown", None)
    if chown is None:
        raise RuntimeError("credential-updater requires POSIX chown support.")
    chown(path, uid, gid)


@dataclass(frozen=True, slots=True)
class UpdateRequest:
    request_id: str
    actor_id: int
    exchange: str
    values: dict[str, str]


@dataclass(frozen=True, slots=True)
class PreviousFile:
    existed: bool
    content: bytes


@dataclass(frozen=True, slots=True)
class UpdaterConfig:
    socket_path: Path
    env_file: Path
    compose_project_dir: Path
    compose_file: Path
    compose_project_name: str
    restart_services: tuple[str, ...]
    allowed_peer_uid: int
    socket_gid: int
    restart_delay_seconds: float
    health_settle_seconds: float

    @classmethod
    def from_env(cls) -> UpdaterConfig:
        project_dir = Path(os.environ.get("CREDENTIAL_COMPOSE_PROJECT_DIR", "/workspace"))
        services = tuple(
            part.strip()
            for part in os.environ.get("CREDENTIAL_RESTART_SERVICES", "bot").split(",")
            if part.strip()
        )
        if (
            not services
            or any(_SERVICE_RE.fullmatch(item) is None for item in services)
            or not set(services).issubset(_ALLOWED_RESTART_SERVICES)
        ):
            raise RuntimeError("CREDENTIAL_RESTART_SERVICES is invalid.")
        project_name = os.environ.get("CREDENTIAL_COMPOSE_PROJECT_NAME", "").strip()
        if _SERVICE_RE.fullmatch(project_name) is None:
            raise RuntimeError("CREDENTIAL_COMPOSE_PROJECT_NAME is required and invalid.")

        config = cls(
            socket_path=Path(
                os.environ.get(
                    "CREDENTIAL_UPDATER_SOCKET", "/run/rebate-bot/credential-updater.sock"
                )
            ),
            env_file=Path(
                os.environ.get("CREDENTIAL_ENV_FILE", "/etc/rebate-bot/exchange-keys.env")
            ),
            compose_project_dir=project_dir,
            compose_file=Path(
                os.environ.get("CREDENTIAL_COMPOSE_FILE", str(project_dir / "docker-compose.yml"))
            ),
            compose_project_name=project_name,
            restart_services=services,
            allowed_peer_uid=int(os.environ.get("CREDENTIAL_ALLOWED_PEER_UID", "10001")),
            socket_gid=int(os.environ.get("CREDENTIAL_SOCKET_GID", "10001")),
            restart_delay_seconds=float(os.environ.get("CREDENTIAL_RESTART_DELAY_SECONDS", "3")),
            health_settle_seconds=float(os.environ.get("CREDENTIAL_HEALTH_SETTLE_SECONDS", "3")),
        )
        for path in (
            config.socket_path,
            config.env_file,
            config.compose_project_dir,
            config.compose_file,
        ):
            if not path.is_absolute():
                raise RuntimeError("Updater paths must be absolute.")
        if config.allowed_peer_uid <= 0 or config.socket_gid < 0:
            raise RuntimeError("Updater peer uid/gid is invalid.")
        if not 0 <= config.restart_delay_seconds <= 30:
            raise RuntimeError("CREDENTIAL_RESTART_DELAY_SECONDS is invalid.")
        if not 0 <= config.health_settle_seconds <= 30:
            raise RuntimeError("CREDENTIAL_HEALTH_SETTLE_SECONDS is invalid.")
        return config


def validate_request(payload: object) -> UpdateRequest:
    if not isinstance(payload, dict):
        raise UpdateRejected("Request must be an object.")
    expected_top_level = {"version", "request_id", "actor_id", "exchange", "values"}
    if set(payload) != expected_top_level or payload.get("version") != 1:
        raise UpdateRejected("Request shape or version is invalid.")

    request_id = payload.get("request_id")
    actor_id = payload.get("actor_id")
    exchange = payload.get("exchange")
    values = payload.get("values")
    try:
        parsed_request_id = str(uuid.UUID(str(request_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise UpdateRejected("request_id is invalid.") from exc
    if not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id <= 0:
        raise UpdateRejected("actor_id is invalid.")
    if not isinstance(exchange, str) or exchange not in _FIELDS_BY_EXCHANGE:
        raise UpdateRejected("Exchange is not allowlisted.")
    if not isinstance(values, dict) or set(values) != _FIELDS_BY_EXCHANGE[exchange]:
        raise UpdateRejected("Credential fields are incomplete or not allowlisted.")
    if not all(
        isinstance(key, str) and isinstance(value, str) and _TOKEN_RE.fullmatch(value) is not None
        for key, value in values.items()
    ):
        raise UpdateRejected("Credential token is malformed.")
    return UpdateRequest(
        request_id=parsed_request_id,
        actor_id=actor_id,
        exchange=exchange,
        values=dict(values),
    )


class EnvFileStore:
    """Strict parser and atomic writer for the dedicated encrypted env file."""

    def __init__(self, path: Path, *, require_root_owner: bool = True) -> None:
        if not path.is_absolute():
            raise ValueError("Credential env path must be absolute.")
        self.path = path
        self._require_root_owner = require_root_owner

    def apply(self, values: dict[str, str]) -> PreviousFile:
        self._validate_location()
        previous = PreviousFile(
            existed=self.path.exists(),
            content=self.path.read_bytes() if self.path.exists() else b"",
        )
        current = self._parse(previous.content)
        current.update(values)
        self._atomic_write(self._serialize(current))
        return previous

    def restore(self, previous: PreviousFile) -> None:
        if previous.existed:
            self._atomic_write(previous.content)
        elif self.path.exists():
            self.path.unlink()
            self._fsync_directory()

    def _validate_location(self) -> None:
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = parent.stat()
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise UpdateRejected("Credential env parent is not a directory.")
        if self._require_root_owner and parent_stat.st_uid != 0:
            raise UpdateRejected("Credential env directory must be owned by root.")
        if os.name != "nt" and stat.S_IMODE(parent_stat.st_mode) != 0o700:
            raise UpdateRejected("Credential env directory permissions must be 0700.")

        if self.path.is_symlink():
            raise UpdateRejected("Credential env file must not be a symlink.")
        if self.path.exists():
            file_stat = self.path.stat()
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                raise UpdateRejected("Credential env target must be one regular file.")
            if self._require_root_owner and file_stat.st_uid != 0:
                raise UpdateRejected("Credential env file must be owned by root.")
            if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) != 0o600:
                raise UpdateRejected("Credential env file permissions must be 0600.")

    @staticmethod
    def _parse(content: bytes) -> dict[str, str]:
        try:
            text = content.decode("ascii")
        except UnicodeDecodeError as exc:
            raise UpdateRejected("Credential env file must be ASCII.") from exc
        values: dict[str, str] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if separator != "=" or key not in _ALL_FIELDS or key in values:
                raise UpdateRejected("Credential env file contains an invalid field.")
            if _TOKEN_RE.fullmatch(value) is None:
                raise UpdateRejected("Credential env file contains a malformed token.")
            values[key] = value
        return values

    @staticmethod
    def _serialize(values: dict[str, str]) -> bytes:
        if not set(values).issubset(_ALL_FIELDS):
            raise UpdateRejected("Credential env contains a non-allowlisted field.")
        lines = [
            "# Managed by credential-updater. Encrypted affiliate credentials only.",
            *(f"{key}={values[key]}" for key in _FIELD_ORDER if key in values),
            "",
        ]
        return "\n".join(lines).encode("ascii")

    def _atomic_write(self, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    fchmod(handle.fileno(), 0o600)
                else:  # Windows test environment; production is Linux-only.
                    os.chmod(temporary, 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            self._fsync_directory()
        finally:
            if temporary.exists():
                temporary.unlink()

    def _fsync_directory(self) -> None:
        if os.name == "nt":  # Directory handles are not fsync-able on Windows.
            return
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class CredentialUpdaterServer:
    def __init__(self, config: UpdaterConfig) -> None:
        self._config = config
        self._store = EnvFileStore(config.env_file)
        self._lock = asyncio.Lock()
        self._seen_request_ids: set[str] = set()
        self._process_lock: BinaryIO | None = None

    async def serve(self) -> None:
        geteuid = getattr(os, "geteuid", None)
        if geteuid is None or geteuid() != 0:
            raise RuntimeError("credential-updater must run as root.")
        self._prepare_socket_directory()
        self._acquire_process_lock()
        self._remove_stale_socket()
        await asyncio.to_thread(self._preflight_services)
        start_unix_server = getattr(asyncio, "start_unix_server", None)
        if start_unix_server is None:
            raise RuntimeError("credential-updater requires Unix socket support.")
        server = await start_unix_server(
            self._handle_connection,
            path=self._config.socket_path,
        )
        _chown(self._config.socket_path, 0, self._config.socket_gid)
        os.chmod(self._config.socket_path, 0o660)
        logger.info("credential updater listening on %s", self._config.socket_path)
        async with server:
            await server.serve_forever()

    def _prepare_socket_directory(self) -> None:
        parent = self._config.socket_path.parent
        parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        parent_stat = parent.stat()
        if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != 0:
            raise RuntimeError("Updater socket directory must be root-owned.")
        if parent_stat.st_mode & 0o022:
            raise RuntimeError("Updater socket directory must not be group/world writable.")
        _chown(parent, 0, self._config.socket_gid)
        os.chmod(parent, 0o750)  # noqa: S103 - group needs socket traversal only

    def _acquire_process_lock(self) -> None:
        lock_path = self._config.socket_path.with_suffix(".lock")
        handle = lock_path.open("a+b")
        os.chmod(lock_path, 0o600)
        fcntl = importlib.import_module("fcntl")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Another credential-updater instance is active.") from exc
        self._process_lock = handle

    def _remove_stale_socket(self) -> None:
        if self._config.socket_path.exists() or self._config.socket_path.is_symlink():
            if not stat.S_ISSOCK(self._config.socket_path.lstat().st_mode):
                raise RuntimeError("Updater socket path exists and is not a socket.")
            self._config.socket_path.unlink()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_id: str | None = None
        try:
            self._verify_peer(writer)
            try:
                raw = await asyncio.wait_for(
                    reader.readline(),
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise UpdateRejected("Request timed out.") from exc
            if not raw or len(raw) > _MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise UpdateRejected("Request is empty or too large.")
            request = validate_request(json.loads(raw))
            request_id = request.request_id

            async with self._lock:
                if request.request_id in self._seen_request_ids:
                    raise UpdateRejected("Duplicate request_id.")
                previous = await asyncio.to_thread(self._store.apply, request.values)
                self._seen_request_ids.add(request.request_id)
                await self._respond(writer, ok=True, request_id=request.request_id)
                logger.info(
                    "credential update accepted request_id=%s actor_id=%s exchange=%s",
                    request.request_id,
                    request.actor_id,
                    request.exchange,
                )
                await asyncio.sleep(self._config.restart_delay_seconds)
                await self._restart_or_rollback(previous, request)
        except (UpdateRejected, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("credential update rejected: %s", type(exc).__name__)
            await self._respond(writer, ok=False, request_id=request_id, error=str(exc))
        except Exception:
            logger.exception("credential updater internal failure")
            await self._respond(
                writer,
                ok=False,
                request_id=request_id,
                error="Credential updater internal failure.",
            )
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def _verify_peer(self, writer: asyncio.StreamWriter) -> None:
        peer_socket = writer.get_extra_info("socket")
        if peer_socket is None or not hasattr(socket, "SO_PEERCRED"):
            raise UpdateRejected("Peer credentials are unavailable.")
        raw = peer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", raw)
        if uid != self._config.allowed_peer_uid:
            raise UpdateRejected("Peer uid is not allowed.")

    async def _restart_or_rollback(
        self,
        previous: PreviousFile,
        request: UpdateRequest,
    ) -> None:
        try:
            await asyncio.to_thread(self._run_compose_recreate)
            logger.info(
                "credential update applied request_id=%s exchange=%s",
                request.request_id,
                request.exchange,
            )
        except Exception:
            logger.exception(
                "service recreation failed; restoring previous credentials request_id=%s",
                request.request_id,
            )
            await asyncio.to_thread(self._store.restore, previous)
            self._seen_request_ids.discard(request.request_id)
            try:
                await asyncio.to_thread(self._run_compose_recreate)
            except Exception:
                logger.exception("rollback recreation failed; operator action required")

    def _run_compose_recreate(self) -> None:
        command = self._compose_command(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            *self._config.restart_services,
        )
        subprocess.run(  # noqa: S603 - executable and arguments are policy-controlled
            command,
            cwd=self._config.compose_project_dir,
            check=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
        )
        time.sleep(self._config.health_settle_seconds)
        running = subprocess.run(  # noqa: S603 - no request data reaches this command
            self._compose_command("ps", "--status", "running", "--services"),
            cwd=self._config.compose_project_dir,
            check=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        running_services = frozenset(running.stdout.split())
        missing = set(self._config.restart_services) - running_services
        if missing:
            raise RuntimeError("One or more recreated services are not running.")

    def _preflight_services(self) -> None:
        configured = subprocess.run(  # noqa: S603 - config allowlists every input
            self._compose_command("config", "--services"),
            cwd=self._config.compose_project_dir,
            check=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        configured_services = frozenset(configured.stdout.split())
        missing = set(self._config.restart_services) - configured_services
        if missing:
            raise RuntimeError("CREDENTIAL_RESTART_SERVICES contains unknown services.")

    def _compose_command(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-directory",
            str(self._config.compose_project_dir),
            "-p",
            self._config.compose_project_name,
            "-f",
            str(self._config.compose_file),
            *arguments,
        ]

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        *,
        ok: bool,
        request_id: str | None,
        error: str | None = None,
    ) -> None:
        if writer.is_closing():
            return
        response: dict[str, Any] = {"ok": ok, "request_id": request_id}
        if error is not None:
            response["error"] = error
        writer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        with suppress(ConnectionError, OSError):
            await writer.drain()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(CredentialUpdaterServer(UpdaterConfig.from_env()).serve())


if __name__ == "__main__":  # pragma: no cover
    main()
