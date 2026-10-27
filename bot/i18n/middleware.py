"""Resolve language without creating users before first-registration attribution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import User as DbUser
from bot.i18n import LANGUAGES, normalize_locale, remember_locale, use_locale


async def saved_locale(session: AsyncSession, user_id: int, fallback: str = "ru") -> str:
    value = await session.scalar(select(DbUser.locale).where(DbUser.id == user_id))
    return value if value in LANGUAGES else fallback


class LocaleMiddleware(BaseMiddleware):
    def __init__(self, staff_ids: frozenset[int]) -> None:
        self._staff_ids = staff_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        session: AsyncSession | None = data.get("session")
        if user is None or session is None:
            return await handler(event, data)
        callback = getattr(data.get("handler"), "callback", None)
        module = getattr(callback, "__module__", "")
        if module.startswith(("bot.admin.", "bot.handlers.credentials")) or (
            module.startswith("bot.support.") and user.id in self._staff_ids
        ):
            with use_locale("ru"):
                return await handler(event, data)
        locale = await saved_locale(session, user.id, normalize_locale(user.language_code))
        remember_locale(user.id, locale)
        with use_locale(locale):
            return await handler(event, data)
