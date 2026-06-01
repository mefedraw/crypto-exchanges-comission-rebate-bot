from __future__ import annotations

import pytest

from bot.utils.validation import UidInputError, validate_uid


def test_validate_uid_ok():
    assert validate_uid(" 1879043947 ") == "1879043947"
    assert validate_uid("abc123XYZ") == "abc123XYZ"


@pytest.mark.parametrize(
    "bad",
    [
        "",            # empty
        "ab",          # too short
        "x" * 33,      # too long
        "12 34",       # space
        "12;34",       # injection char
        "uid_1",       # underscore
        "1/2",         # slash
        "日本",         # non-ascii
    ],
)
def test_validate_uid_rejects(bad):
    with pytest.raises(UidInputError):
        validate_uid(bad)
