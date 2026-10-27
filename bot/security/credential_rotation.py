"""Safe owner-initiated rotation of affiliate API credentials.

Plaintext credentials exist only while handling the owner's final Telegram
message. They are validated with a real read-only adapter request, encrypted,
and sent to the isolated credential updater over a Unix socket.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import SecretStr

from bot.config import ExchangeCredentials, ExchangeSpec, Settings
from bot.exchanges.base import ExchangeApiError
from bot.exchanges.registry import build_adapter_with_credentials
from bot.security.secrets import SecretCipher
from bot.utils.dates import day_end, day_start

_MAX_SECRET_LENGTH = 512
_MAX_RESPONSE_BYTES = 16 * 1024


class CredentialInputError(ValueError):
    """The Telegram message does not contain a valid credential bundle."""


class CredentialValidationError(RuntimeError):
    """The exchange rejected the new credentials or the verification request."""


class CredentialUpdaterError(RuntimeError):
    """The isolated updater did not accept the encrypted credential bundle."""


@dataclass(frozen=True, slots=True, repr=False)
class CredentialInput:
    """One complete plaintext credential set, deliberately hidden from repr."""

    spec: ExchangeSpec
    api_key: SecretStr
    api_secret: SecretStr
    passphrase: SecretStr | None = None

    def as_exchange_credentials(self) -> ExchangeCredentials:
        return ExchangeCredentials(
            spec=self.spec,
            api_key=self.api_key,
            api_secret=self.api_secret,
            passphrase=self.passphrase,
        )

    def encrypted_env(self, cipher: SecretCipher) -> dict[str, str]:
        prefix = self.spec.env_prefix
        values = {
            f"{prefix}_API_KEY": cipher.encrypt(self.api_key.get_secret_value()),
            f"{prefix}_API_SECRET": cipher.encrypt(self.api_secret.get_secret_value()),
        }
        if self.spec.requires_passphrase:
            if self.passphrase is None:  # Defensive: parser always enforces this.
                raise CredentialInputError("Для этой биржи требуется passphrase.")
            values[f"{prefix}_API_PASSPHRASE"] = cipher.encrypt(self.passphrase.get_secret_value())
        return values


@dataclass(frozen=True, slots=True)
class RotationReceipt:
    request_id: str
    masked_api_key: str


class CredentialUpdater(Protocol):
    async def apply(
        self,
        exchange: str,
        encrypted_values: dict[str, str],
        *,
        actor_id: int,
    ) -> str: ...


def _validate_secret_part(value: str) -> str:
    if not value or len(value) > _MAX_SECRET_LENGTH:
        raise CredentialInputError("Поле credentials пустое или слишком длинное.")
    if not value.isascii() or not value.isprintable() or any(char.isspace() for char in value):
        raise CredentialInputError(
            "Credentials должны состоять из печатных ASCII-символов без пробелов."
        )
    return value


def parse_credential_message(spec: ExchangeSpec, text: str) -> CredentialInput:
    """Parse one message containing key, secret and optional passphrase by line."""
    parts = text.splitlines()
    expected = 1 + int(spec.requires_secret) + int(spec.requires_passphrase)
    if len(parts) != expected:
        raise CredentialInputError(f"Ожидалось строк: {expected}.")

    validated = [_validate_secret_part(part) for part in parts]
    api_key = validated[0]
    index = 1
    if spec.requires_secret:
        api_secret = validated[index]
        index += 1
    else:
        api_secret = ""
    passphrase = validated[index] if spec.requires_passphrase else None

    return CredentialInput(
        spec=spec,
        api_key=SecretStr(api_key),
        api_secret=SecretStr(api_secret),
        passphrase=SecretStr(passphrase) if passphrase is not None else None,
    )


def credential_prompt(spec: ExchangeSpec) -> str:
    fields = ["API key"]
    if spec.requires_secret:
        fields.append("API secret")
    if spec.requires_passphrase:
        fields.append("API passphrase")
    layout = "\n".join(f"{number}. <code>{field}</code>" for number, field in enumerate(fields, 1))
    return (
        f"Отправьте credentials для <b>{spec.label}</b> одним сообщением, "
        "каждое значение с новой строки:\n\n"
        f"{layout}\n\n"
        "Сообщение будет немедленно удалено."
    )


def mask_api_key(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return f"****{value[-4:]}"


class UnixCredentialUpdater:
    """Minimal newline-delimited JSON client for the root updater."""

    def __init__(self, socket_path: str, *, timeout_seconds: float = 10.0) -> None:
        path = PurePosixPath(socket_path)
        if not path.is_absolute():
            raise ValueError("Credential updater socket path must be absolute.")
        self._socket_path = str(path)
        self._timeout_seconds = timeout_seconds

    async def apply(
        self,
        exchange: str,
        encrypted_values: dict[str, str],
        *,
        actor_id: int,
    ) -> str:
        request_id = str(uuid.uuid4())
        payload = (
            json.dumps(
                {
                    "version": 1,
                    "request_id": request_id,
                    "actor_id": actor_id,
                    "exchange": exchange,
                    "values": encrypted_values,
                },
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )

        open_unix_connection = getattr(asyncio, "open_unix_connection", None)
        if open_unix_connection is None:
            raise CredentialUpdaterError("Unix sockets недоступны на этой платформе.")
        try:
            reader, writer = await asyncio.wait_for(
                open_unix_connection(self._socket_path),
                timeout=self._timeout_seconds,
            )
        except (TimeoutError, OSError) as exc:
            raise CredentialUpdaterError("Сервис обновления credentials недоступен.") from exc

        try:
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), timeout=self._timeout_seconds)
            response = await asyncio.wait_for(reader.readline(), timeout=self._timeout_seconds)
            if not response or len(response) > _MAX_RESPONSE_BYTES:
                raise CredentialUpdaterError("Некорректный ответ сервиса обновления.")
            parsed = json.loads(response)
            if not isinstance(parsed, dict) or parsed.get("ok") is not True:
                error = parsed.get("error") if isinstance(parsed, dict) else None
                raise CredentialUpdaterError(str(error or "Обновление отклонено."))
            if parsed.get("request_id") != request_id:
                raise CredentialUpdaterError("Сервис вернул неверный request_id.")
            return request_id
        except CredentialUpdaterError:
            raise
        except (TimeoutError, OSError, ValueError) as exc:
            raise CredentialUpdaterError("Не удалось подтвердить обновление credentials.") from exc
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


class CredentialRotationService:
    """Validate a complete bundle, encrypt it and hand it to the updater."""

    def __init__(self, settings: Settings, updater: CredentialUpdater) -> None:
        if settings.master_key is None:
            raise RuntimeError("MASTER_KEY is required for credential rotation.")
        self._settings = settings
        self._cipher = SecretCipher(settings.master_key.get_secret_value())
        self._updater = updater

    async def rotate(
        self,
        bundle: CredentialInput,
        *,
        verification_uid: str,
        actor_id: int,
    ) -> RotationReceipt:
        adapter = build_adapter_with_credentials(
            self._settings,
            bundle.as_exchange_credentials(),
        )
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        try:
            await adapter.get_commission(
                verification_uid,
                day_start(yesterday),
                day_end(yesterday),
            )
        except ExchangeApiError as exc:
            raise CredentialValidationError(
                "Биржа отклонила проверочный запрос. Credentials не изменены."
            ) from exc
        finally:
            aclose = getattr(adapter, "aclose", None)
            if aclose is not None:
                await aclose()

        encrypted_values = bundle.encrypted_env(self._cipher)
        request_id = await self._updater.apply(
            bundle.spec.code,
            encrypted_values,
            actor_id=actor_id,
        )
        return RotationReceipt(
            request_id=request_id,
            masked_api_key=mask_api_key(bundle.api_key.get_secret_value()),
        )
