"""Validation of untrusted user input before it touches URLs or signatures.

uid is the only free-form value forwarded to exchange APIs, so it is restricted
to a conservative character whitelist and length bounds.
"""

from __future__ import annotations

import re

_UID_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
_UID_MIN_LEN = 3
_UID_MAX_LEN = 32


class UidInputError(ValueError):
    """Raised when a uid fails validation."""


def validate_uid(raw: str) -> str:
    """Return the cleaned uid or raise :class:`UidInputError`.

    Allows only ASCII letters and digits — no spaces, separators, or symbols —
    which covers every supported exchange's uid format and blocks injection.
    """
    uid = raw.strip()
    if not (_UID_MIN_LEN <= len(uid) <= _UID_MAX_LEN):
        raise UidInputError(
            f"UID должен быть длиной от {_UID_MIN_LEN} до {_UID_MAX_LEN} символов."
        )
    if not _UID_PATTERN.match(uid):
        raise UidInputError("UID может содержать только латинские буквы и цифры.")
    return uid
