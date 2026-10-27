"""Reference (source-of-truth) redaction for logs shipped off-box.

The Fluent Bit agent (ops/fluent-bit/redact.lua) redacts in Lua before pushing to
Grafana Cloud; this module is the authoritative Python spec of WHAT must be redacted,
covered by tests. The Lua mirrors these behaviors (kept in sync by hand; see
ops/fluent-bit/README.md). Secrets are already masked in-app — this is defense-in-depth
because logs leave the server.
"""

from __future__ import annotations

import re

# A run of `0x` followed by hex. The callback decides by LENGTH: exactly 40 hex is an
# EVM address (redact); 64 hex is a txhash (keep — we want it in logs); a masked address
# uses an ellipsis so it is not a contiguous hex run and never matches.
_HEX0X = re.compile(r"0x[0-9a-fA-F]+")
# key=value / key: value where the key is a secret. Case-insensitive.
_SECRET_KV = re.compile(
    r"(?i)\b(private_key|master_key|api_secret|passphrase|bot_token|secret|seed|authorization)"
    r"(\s*[=:]\s*)"
    r"((?:bearer|basic|token|digest)\s+)?"
    r"(\S+)"
)
# JSON-quoted key/value form (production structlog output), e.g. `"api_secret":"x"` or
# `"authorization":"Bearer sk-..."` -> `"<key>":"<redacted>"`. Mirrors the same keyword
# list as _SECRET_KV; kept as a separate pattern since the Lua side also handles the two
# forms independently.
_SECRET_KV_JSON = re.compile(
    r'"(private_key|master_key|api_secret|passphrase|bot_token|secret|seed|authorization)"'
    r'\s*:\s*"[^"]*"'
)


def redact_line(text: str) -> str:
    def _addr(m: re.Match[str]) -> str:
        tok = m.group(0)
        return "0x<redacted>" if len(tok) - 2 == 40 else tok

    text = _HEX0X.sub(_addr, text)
    text = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    text = _SECRET_KV_JSON.sub(lambda m: f'"{m.group(1)}":"<redacted>"', text)
    return text


def test_full_evm_address_is_redacted():
    addr = "0x" + "a1b2c3d4" * 5  # exactly 40 hex chars
    assert redact_line(f"paying {addr} now") == "paying 0x<redacted> now"


def test_txhash_is_preserved():
    txh = "0x" + "ab" * 32  # 64 hex chars
    assert redact_line(f"broadcast txhash={txh}") == f"broadcast txhash={txh}"


def test_masked_address_is_preserved():
    line = "wallet 0x2783…Ef72 balance ok"
    assert redact_line(line) == line


def test_bot_token_value_is_redacted():
    assert redact_line("start bot_token=123456:AAbbCC done") == "start bot_token=<redacted> done"


def test_api_secret_colon_form_is_redacted():
    assert redact_line("cfg api_secret: deadbeefcafe") == "cfg api_secret: <redacted>"


def test_authorization_bearer_token_is_redacted():
    line = "req authorization: Bearer sk-secret-abc123 done"
    assert redact_line(line) == "req authorization: <redacted> done"


def test_plain_message_is_unchanged():
    line = "payout confirmed & paid pid=42 confirmations=15"
    assert redact_line(line) == line


# --- production JSON (structlog JSONRenderer) format ---


def test_json_full_evm_address_is_redacted():
    addr = "0x" + "a1b2c3d4" * 5  # exactly 40 hex chars
    line = f'{{"event":"paying out","address":"{addr}","level":"info"}}'
    expected = '{"event":"paying out","address":"0x<redacted>","level":"info"}'
    assert redact_line(line) == expected


def test_json_api_secret_is_redacted():
    line = '{"event":"auth","api_secret":"deadbeefcafe"}'
    assert redact_line(line) == '{"event":"auth","api_secret":"<redacted>"}'


def test_json_authorization_bearer_is_redacted():
    line = '{"authorization":"Bearer sk-secret-abc123"}'
    assert redact_line(line) == '{"authorization":"<redacted>"}'


def test_json_txhash_is_preserved():
    txh = "0x" + "ab" * 32  # 64 hex chars
    line = f'{{"event":"broadcast","txhash":"{txh}","level":"info"}}'
    assert redact_line(line) == line
