"""Developer alerting: push failures to the developer's Telegram chat.

Alerts are best-effort — a failure to deliver an alert must never crash the bot
or mask the original error, so every send is wrapped and logged on failure.

Tracebacks are trimmed and length-capped before sending. They are not run through
the log redactor, so we deliberately avoid putting request URLs/headers into
alert text (those can carry signed query strings); only the exception type and
message plus a short traceback are sent.
"""

from __future__ import annotations

import traceback
from html import escape

from aiogram import Bot

from bot.logging import get_logger

logger = get_logger(__name__)

_MAX_TRACEBACK_CHARS = 1500
_TELEGRAM_LIMIT = 4096


class Alerter:
    """Sends alert messages to the developer chat."""

    def __init__(self, bot: Bot, developer_id: int) -> None:
        self._bot = bot
        self._developer_id = developer_id

    async def send(self, title: str, detail: str | None = None) -> None:
        """Send a plain alert. Never raises."""
        text = f"🚨 <b>{escape(title)}</b>"
        if detail:
            text += f"\n{escape(detail)}"
        await self._deliver(text)

    async def alert_exception(
        self, title: str, exc: BaseException, *, context: str | None = None
    ) -> None:
        """Send an exception alert with a trimmed, escaped traceback. Never raises."""
        parts = [f"🚨 <b>{escape(title)}</b>"]
        if context:
            parts.append(escape(context))
        parts.append(f"<code>{escape(type(exc).__name__)}: {escape(str(exc))}</code>")

        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if len(tb) > _MAX_TRACEBACK_CHARS:
            tb = "…" + tb[-_MAX_TRACEBACK_CHARS:]
        parts.append(f"<pre>{escape(tb)}</pre>")

        await self._deliver("\n".join(parts))

    async def _deliver(self, text: str) -> None:
        if len(text) > _TELEGRAM_LIMIT:
            text = text[: _TELEGRAM_LIMIT - 1] + "…"
        try:
            await self._bot.send_message(self._developer_id, text)
        except Exception as exc:  # noqa: BLE001 - alerting must not raise
            logger.warning("failed to deliver alert", error=type(exc).__name__)
