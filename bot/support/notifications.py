"""Resolve durable case notifications without putting message bodies into logs."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db.models.ops import Notification
from bot.db.models.support import SupportCase, SupportMessage
from bot.i18n import gettext as tr
from bot.support import keyboards, rendering, service, statuses


async def render(
    session: AsyncSession, notification: Notification
) -> tuple[str, InlineKeyboardMarkup] | None:
    payload = notification.payload or {}
    case_id = payload.get("case_id")
    if not isinstance(case_id, int):
        return None
    case = await session.get(SupportCase, case_id)
    if case is None:
        return None
    owner = notification.recipient_chat_id == case.user_id
    staff = notification.recipient_chat_id in get_settings().admin_telegram_ids
    if not staff and not owner:
        return None
    if notification.kind == service.CLOSED:
        if case.status != statuses.CLOSED:
            return None
        text = tr(
            "<b>Обращение №{v0}: кейс закрыт.</b>\n\nОткройте кейс и " "ознакомьтесь с перепиской."
        ).format(v0=case.id)
    else:
        message_id = payload.get("message_id")
        message = (
            await session.get(SupportMessage, message_id) if isinstance(message_id, int) else None
        )
        if message is None or message.case_id != case.id:
            return None
        if owner:
            if (
                notification.kind not in {service.ACTION_REQUIRED, service.REPLIED}
                or not message.is_staff
                or case.status != statuses.REQUIRES_REPLY
            ):
                return None
            latest = await session.scalar(
                select(func.max(SupportMessage.id)).where(SupportMessage.case_id == case.id)
            )
            if message.id != latest:
                return None
            return (
                tr(
                    "<b>По обращению №{v0} требуется ваш ответ.</b>\n\nОткройте кейс и "
                    "ознакомьтесь с сообщением поддержки."
                ).format(v0=case.id),
                keyboards.notification(case.id),
            )
        if notification.kind not in {service.OPENED, service.REPLIED}:
            return None
        text = (
            rendering.case_request(case, message.body)
            if notification.kind == service.OPENED
            else rendering.reply(case.id, message.body, staff=message.is_staff)
        )
    return text, keyboards.notification(case.id) if owner else keyboards.case_actions(
        case, staff=True
    )
