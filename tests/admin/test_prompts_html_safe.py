"""Regression: admin input prompts are sent with parse_mode=HTML, so a literal
"<ник>"/"<user_id>" made Telegram reject the message ("Unsupported start tag").
Placeholders must be escaped as &lt;…&gt;; only a small set of real formatting
tags may appear unescaped."""

from __future__ import annotations

import re

import pytest

from bot.admin import router

_PROMPTS = [
    router._RATE_PROMPT,
    router._USER_STATUS_PROMPT,
    router._REASSIGN_PROMPT,
    router._BLOCK_UID_PROMPT,
]

# Telegram-HTML tags we intentionally use in prompts. Anything else inside <…>
# is an unescaped placeholder (the original bug) and must fail.
_ALLOWED_TAGS = {"b", "/b", "code", "/code", "i", "/i"}


@pytest.mark.parametrize("prompt", _PROMPTS)
def test_admin_prompt_has_only_allowed_html_tags(prompt: str) -> None:
    # Escaped placeholders are "&lt;…&gt;", so they never match this; only real
    # "<tag>" sequences do. A raw "<ник или user_id>" would surface here as an
    # illegal tag token and fail — exactly the bug we guard against.
    for token in re.findall(r"<([^>]*)>", prompt):
        assert token in _ALLOWED_TAGS, f"illegal/raw tag <{token}> in prompt: {prompt!r}"


def test_prompts_still_show_placeholders_as_entities() -> None:
    assert "&lt;" in router._RATE_PROMPT and "&gt;" in router._RATE_PROMPT
    assert "&lt;ник или user_id&gt;" in router._USER_STATUS_PROMPT
    assert "&lt;биржа&gt;" in router._REASSIGN_PROMPT


def test_rate_prompt_explains_percent_and_base() -> None:
    # Part 2: the prompt must explain it's a percent, what it applies to, and the cap.
    p = router._RATE_PROMPT
    assert "процент" in p.lower()
    assert "комисси" in p.lower()  # explains it's the bot's commission
    assert "0-100" in p


def test_pct_str_renders_fraction_as_clean_percent() -> None:
    from decimal import Decimal

    assert router._pct_str(Decimal("0.02")) == "2"
    assert router._pct_str(Decimal("0.025")) == "2.5"
    assert router._pct_str(Decimal("0.10")) == "10"
    assert router._pct_str(Decimal("0")) == "0"
