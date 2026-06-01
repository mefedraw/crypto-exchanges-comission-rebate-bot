"""Access control: only whitelisted Telegram users may interact with the bot.

Implemented as an *outer* middleware so it runs before filters/handlers on every
update type. Updates from anyone else are dropped silently (no reply, so the bot's
existence isn't confirmed) and the attempt is logged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from bot.logging import get_logger

logger = get_logger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class WhitelistMiddleware(BaseMiddleware):
    """Reject any update whose originating user is not in the allow-list."""

    def __init__(self, allowed_ids: frozenset[int]) -> None:
        self._allowed = allowed_ids

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id not in self._allowed:
            # Drop silently; log enough to audit, never enough to leak.
            logger.warning(
                "rejected non-whitelisted update",
                user_id=getattr(user, "id", None),
                update_type=type(event).__name__,
            )
            return None
        return await handler(event, data)
