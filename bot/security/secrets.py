"""At-rest secret handling via Fernet (symmetric authenticated encryption).

Two responsibilities:

* :class:`SecretCipher` — decrypt exchange credentials at runtime. The master key
  lives only in process memory (passed in from the ``MASTER_KEY`` env var) and is
  never persisted or logged.
* A tiny CLI (``python -m bot.security.secrets ...``) to generate a master key and
  to encrypt credentials once, when preparing the deployment ``.env``.

Plaintext secrets are never printed except by the explicit ``decrypt`` subcommand,
which a human runs deliberately.
"""

from __future__ import annotations

import sys

from cryptography.fernet import Fernet, InvalidToken


class SecretError(RuntimeError):
    """Raised when a master key or token is malformed or cannot be decrypted."""


class SecretCipher:
    """Wraps a Fernet master key to encrypt/decrypt short credential strings."""

    def __init__(self, master_key: str) -> None:
        try:
            self._fernet = Fernet(master_key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise SecretError(
                "Invalid MASTER_KEY: expected a urlsafe-base64 Fernet key "
                "(generate one with `python -m bot.security.secrets keygen`)."
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretError(
                "Failed to decrypt a secret: wrong MASTER_KEY or corrupted token."
            ) from exc


def _read_secret(prompt: str) -> str:
    """Read a secret from a TTY without echoing; fall back to stdin when piped."""
    import getpass

    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\n")


def _main(argv: list[str]) -> int:
    usage = (
        "Usage:\n"
        "  python -m bot.security.secrets keygen          # print a new Fernet master key\n"
        "  python -m bot.security.secrets encrypt         # encrypt a secret (reads stdin)\n"
        "  python -m bot.security.secrets decrypt         # decrypt a token (reads stdin)\n"
        "\n"
        "encrypt/decrypt read MASTER_KEY from the environment."
    )
    if len(argv) != 1 or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 2 if argv else 0

    command = argv[0]

    if command == "keygen":
        print(Fernet.generate_key().decode("ascii"))
        return 0

    if command in {"encrypt", "decrypt"}:
        import os

        master_key = os.environ.get("MASTER_KEY")
        if not master_key:
            print("MASTER_KEY is not set in the environment.", file=sys.stderr)
            return 1
        cipher = SecretCipher(master_key)
        try:
            if command == "encrypt":
                print(cipher.encrypt(_read_secret("Secret to encrypt: ")))
            else:
                print(cipher.decrypt(_read_secret("Token to decrypt: ")))
        except SecretError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    print(f"Unknown command: {command}\n\n{usage}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv[1:]))
