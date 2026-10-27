# bot/notifications/poller.py
"""Bot-side delivery of the notification outbox.

The bot process (which holds the Telegram token) polls undelivered notifications
written by the executor/accrual processes and sends them. Delivery is idempotent:
a sent row is stamped ``delivered_at``; a failed send bumps ``attempts`` and is
retried next cycle (up to the repository's cap). A send failure never aborts the
cycle or the loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from html import escape

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import get_settings
from bot.db.models.ops import Notification
from bot.i18n import gettext as tr
from bot.i18n import use_locale
from bot.i18n.middleware import saved_locale
from bot.logging import get_logger
from bot.notifications import repository as repo
from bot.partners.rank_control import RANK_CHANGED
from bot.partners.ranks import rank_by_level
from bot.support import notifications as support_notifications
from bot.support.service import KINDS as SUPPORT_KINDS
from bot.utils.money import format_2dp

logger = get_logger(__name__)


def _explorer(chain_id: int) -> str:
    return "https://testnet.bscscan.com" if chain_id == 97 else "https://bscscan.com"


def _net(payload: dict[str, object]) -> str:
    """User-facing amount, 2dp — payloads carry full-precision Numeric strings."""
    try:
        return format_2dp(Decimal(str(payload.get("net_usdt", "0"))))
    except (InvalidOperation, ValueError):
        return "0.00"


def _amount_and_unit(payload: dict[str, object], *, floor: bool = False) -> tuple[str, str]:
    """FINDING 2: pick the right amount + unit for the payout's asset.

    MNT payouts carry a USDT-equivalent in ``net_usdt`` (used for the auto-limit/
    min-payout thresholds) but the user must be told the actual MNT amount that
    moved — ``net_native`` — never a USDT figure. USDT payouts are unchanged.

    FIX M1: ``net_native`` is the raw (un-floored) accrual figure, but
    ``native_to_wei`` floors to whole cents (ROUND_DOWN) before broadcasting — the
    amount actually sent. ``format_2dp`` rounds ROUND_HALF_UP, so e.g. 12.345 would
    be shown as "12.35 MNT" when only 12.34 was sent. ``floor=True`` (used for the
    PAID notification, where money has actually moved) matches that same ROUND_DOWN
    so the paid-notification amount always equals what was broadcast. PAYOUT_FAILED
    (nothing sent) keeps the unfloored, exact figure — unchanged.
    """
    if payload.get("asset") == "MNT":
        try:
            amount = Decimal(str(payload.get("net_native", "0")))
        except (InvalidOperation, ValueError):
            return "0.00", "MNT"
        if floor:
            amount = amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        return format_2dp(amount), "MNT"
    return _net(payload), "USDT"


def _gas(payload: dict[str, object]) -> str | None:
    """Gas deduction shown to 4dp (it's cents-scale), or None if zero/absent."""
    try:
        gas = Decimal(str(payload.get("gas_usdt", "0")))
    except (InvalidOperation, ValueError):
        return None
    return f"{gas:.4f}" if gas > 0 else None


def format_message(notification: Notification, chain_id: int) -> str:
    """Render a notification row into user/developer-facing HTML text."""
    payload = notification.payload or {}
    if notification.kind == RANK_CHANGED:
        try:
            previous = rank_by_level(int(payload["previous_rank"]))
            current = rank_by_level(int(payload["rank"]))
        except (KeyError, TypeError, ValueError):
            return tr("Партнерский ранг изменен.")
        return tr(
            "<b>Партнерский ранг изменен</b>\n\nРанг: {v0} → <b>{v1}</b>\nДоля "
            "нового дохода владельца: <b>{v2:.0f}%</b>\nРанее начисленные суммы"
            " не изменились."
        ).format(v0=previous.level, v1=current.level, v2=current.share * 100)
    if notification.kind == repo.PAYOUT_PAID:
        amount, unit = _amount_and_unit(payload, floor=True)
        txhash = str(payload.get("txhash") or "")
        text = tr("✅ Средства дошли до получателя!\nОтправлено <b>{v0} {v1}</b>.").format(
            v0=amount, v1=unit
        )
        gas = _gas(payload)
        if gas:
            text += tr("\n(газ сети {v0} USDT удержан)").format(v0=gas)
        if txhash:
            text += f"\n🔗 {_explorer(chain_id)}/tx/{txhash}"
        return text
    if notification.kind == repo.PAYOUT_FAILED:
        amount, unit = _amount_and_unit(payload)
        return tr(
            "⚠️ Не удалось отправить выплату <b>{v0} {v1}</b>. Средства не "
            "списаны — начисления возвращены на баланс, попробуйте позже."
        ).format(v0=amount, v1=unit)
    # dev_alert
    title = escape(str(payload.get("title", "alert")))
    detail = escape(str(payload.get("detail", "")))
    return f"🚨 <b>{title}</b>\n{detail}" if detail else f"🚨 <b>{title}</b>"


async def deliver_pending(
    bot: Bot,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    chain_id: int,
    now: Callable[[], datetime],
) -> int:
    """Run one delivery cycle. Returns the number of notifications sent."""
    sent = 0
    async with sessionmaker() as session:
        for notification in await repo.claim_undelivered(session):
            try:
                locale = await saved_locale(session, notification.recipient_chat_id)
                if notification.kind == repo.DEV_ALERT or (
                    notification.kind in SUPPORT_KINDS
                    and notification.recipient_chat_id in get_settings().admin_telegram_ids
                ):
                    locale = "ru"
                with use_locale(locale):
                    if notification.kind in SUPPORT_KINDS:
                        rendered = await support_notifications.render(session, notification)
                        if rendered is None:
                            await repo.mark_delivered(session, notification.id, now())
                            continue
                        text, keyboard = rendered
                        await bot.send_message(
                            notification.recipient_chat_id,
                            text,
                            reply_markup=keyboard,
                            disable_web_page_preview=True,
                        )
                    else:
                        await bot.send_message(
                            notification.recipient_chat_id,
                            format_message(notification, chain_id),
                        )
            except Exception as exc:
                logger.warning(
                    "notification send failed",
                    nid=notification.id,
                    kind=notification.kind,
                    error=type(exc).__name__,
                )
                await repo.bump_attempt(session, notification.id)
                continue
            await repo.mark_delivered(session, notification.id, now())
            sent += 1
        await session.commit()
    return sent


async def run_forever(
    bot: Bot,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    chain_id: int,
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], datetime],
    interval_seconds: float = 10.0,
    max_cycles: int | None = None,
) -> int:
    """Poll and deliver notifications forever (or ``max_cycles`` times in tests)."""
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        try:
            await deliver_pending(bot, sessionmaker, chain_id=chain_id, now=now)
        except Exception:  # a bad cycle must not kill the loop
            logger.exception("notification_poller_cycle_failed")
        cycles += 1
        await sleep(interval_seconds)
    return cycles
