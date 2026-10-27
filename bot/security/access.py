"""Access control for a public bot: everyone may interact EXCEPT users an admin
has blocked.

The bot is open — any Telegram user can register a UID/wallet and claim their
referral rebate. The only gate is the per-user ``users.status`` an admin controls:
a ``blocked`` account is turned away at the door (in addition to the existing
block checks on the withdrawal/payout path). Admins and the developer are never
blocked, so staff can always recover the bot.

Implemented as an *inner* middleware on message/callback updates, registered AFTER
:class:`~bot.botui.middleware.DbSessionMiddleware` so ``data["session"]`` is
available for the status lookup. Non-user updates (no ``event_from_user``) pass
through untouched.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from bot.botui.repository import get_user_status
from bot.i18n import gettext as tr
from bot.logging import get_logger

logger = get_logger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

_BLOCKED_NOTICE = "🚫 Ваш аккаунт заблокирован. Обратитесь к администратору."


class BlockGuardMiddleware(BaseMiddleware):
    """Allow every user through except DB-``blocked`` ones; staff are exempt."""

    def __init__(self, exempt_ids: frozenset[int]) -> None:
        # Admins + developer — never turned away, so they can always unblock others.
        self._exempt = exempt_ids

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id in self._exempt:
            return await handler(event, data)
        session = data.get("session")
        if session is not None and (await get_user_status(session, user.id)) == "blocked":
            logger.info("blocked user rejected", user_id=user.id)
            await _notify_blocked(event)
            return None
        return await handler(event, data)


async def _notify_blocked(event: TelegramObject) -> None:
    """Best-effort "you're blocked" notice; never let a send failure break the update."""
    with contextlib.suppress(Exception):
        if isinstance(event, CallbackQuery):
            await event.answer(tr(_BLOCKED_NOTICE), show_alert=True)
        elif isinstance(event, Message):
            await event.answer(tr(_BLOCKED_NOTICE))
