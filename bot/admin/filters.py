# bot/admin/filters.py
"""Admin gate.

Design note (Phase 6): in aiogram 3, *inner* middlewares (our
``DbSessionMiddleware``) run AFTER filters, so a filter cannot depend on
``data["session"]``. ``IsAdmin`` therefore checks membership in the config's
``admin_telegram_ids`` frozenset directly — that set is the actual source of
truth for who is admin. The DB ``users.role == 'admin'`` flag is set at
bootstrap (Task 7) for record-keeping only and is never read here.
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject


class IsAdmin(BaseFilter):
    """True iff the update's originating user is in the configured admin set."""

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self._admin_ids = admin_ids

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in self._admin_ids
