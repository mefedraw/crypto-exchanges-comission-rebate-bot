"""Database configuration, read from the environment."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class DbSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://bot:bot@localhost:5432/bot"
