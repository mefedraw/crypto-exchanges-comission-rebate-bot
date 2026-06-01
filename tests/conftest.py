"""Shared test helpers."""

from __future__ import annotations

from typing import Any

from bot.config import Settings


def make_settings(**aliases: Any) -> Settings:
    """Build a Settings instance from explicit values, ignoring any local .env.

    Values are passed by their env alias (e.g. ``BOT_TOKEN``). Sensible defaults
    are provided for the always-required fields.
    """
    defaults: dict[str, Any] = {
        "BOT_TOKEN": "123:abc",
        "USER_TELEGRAM_ID": "111",
        "DEVELOPER_TELEGRAM_ID": "222",
    }
    defaults.update(aliases)
    return Settings(_env_file=None, **defaults)
