"""Shared test helpers."""

from __future__ import annotations

from typing import Any

from bot.config import Settings


def make_settings(**aliases: Any) -> Settings:
    """Build a Settings instance from explicit values, ignoring any local .env.

    Values are passed by their env alias (e.g. ``BOT_TOKEN``) or field name
    (e.g. ``bot_token``). Field names are automatically converted to aliases.
    Sensible defaults are provided for the always-required fields.
    """
    defaults: dict[str, Any] = {
        "BOT_TOKEN": "123:abc",
        "USER_TELEGRAM_IDS": "111",
        "DEVELOPER_TELEGRAM_ID": "222",
    }
    # Convert snake_case field names to UPPER_CASE aliases
    for key, value in list(aliases.items()):
        if key.islower():
            # Convert snake_case to UPPER_CASE
            alias_key = key.upper()
            aliases[alias_key] = aliases.pop(key)
    defaults.update(aliases)
    return Settings(_env_file=None, **defaults)
