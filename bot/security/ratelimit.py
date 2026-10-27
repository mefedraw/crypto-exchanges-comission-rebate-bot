"""Per-user anti-flood throttling.

Guards against accidental spam and against burning through exchange API rate
limits. Enforces a minimum interval between *accepted* messages/callbacks per
user; faster events are dropped with a brief, throttled notice.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from bot.i18n import gettext as tr
from bot.i18n import recent_locale, use_locale
from bot.logging import get_logger

logger = get_logger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class RateLimitMiddleware(BaseMiddleware):
    """Allow at most one accepted event per ``min_interval`` seconds per user."""

    def __init__(
        self,
        min_interval: float,
        *,
        bypass_states: frozenset[str] = frozenset(),
    ) -> None:
        self._min_interval = min_interval
        self._bypass_states = bypass_states
        self._last_seen: dict[int, float] = {}

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        raw_state = data.get("raw_state")
        state: FSMContext | None = data.get("state")
        if raw_state is None and state is not None:
            raw_state = await state.get_state()
        if isinstance(raw_state, str) and raw_state in self._bypass_states:
            return await handler(event, data)

        now = time.monotonic()
        last = self._last_seen.get(user.id)
        if last is not None and (now - last) < self._min_interval:
            await self._notify_throttled(event)
            return None

        self._last_seen[user.id] = now
        return await handler(event, data)

    async def _notify_throttled(self, event: TelegramObject) -> None:
        """Give quiet feedback without spamming back."""
        user = getattr(event, "from_user", None)
        locale = recent_locale(user.id, getattr(user, "language_code", None)) if user else "en"
        with use_locale(locale):
            if isinstance(event, CallbackQuery):
                await event.answer(tr("Слишком часто — подождите немного."), show_alert=False)
            elif isinstance(event, Message):
                await event.answer(tr("Слишком часто — подождите немного."))
