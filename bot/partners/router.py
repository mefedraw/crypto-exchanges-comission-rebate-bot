"""Private partner dashboard, rank criteria, and explicit payout confirmation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from html import escape

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import panel
from bot.botui.keyboards import CabinetCB
from bot.botui.service import WithdrawBlocked
from bot.config import get_settings
from bot.db.models.partners import PartnerProgram, PartnerRankReview
from bot.i18n import gettext as tr
from bot.partners import rank_control, service, withdrawal
from bot.partners.ranks import RANKS, THRESHOLDS
from bot.payout.expiry import PAYOUT_TZ

router = Router(name="partner-program")
router.callback_query.filter(F.message.chat.type == "private")


class PartnerCB(CallbackData, prefix="partner"):
    action: str
    asset: str = "USDT"
    token: str = "-"


def amount(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _back() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("Партнерская программа"), callback_data=CabinetCB(action="partner"))
    b.button(text=tr("Пригласить друга"), callback_data=CabinetCB(action="invite"))
    b.button(text=tr("В меню"), callback_data=CabinetCB(action="menu"))
    b.adjust(1)
    return b.as_markup()


def _keyboard(summary: service.PartnerSummary) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("Условия рангов"), callback_data=PartnerCB(action="ranks"))
    b.button(text=tr("История ранга"), callback_data=PartnerCB(action="history"))
    b.button(text=tr("Вывести партнерские USDT"), callback_data=PartnerCB(action="withdraw"))
    if summary.balances["MNT"].available_native > 0 and get_settings().mnt_enabled:
        b.button(
            text=tr("Вывести партнерские MNT"),
            callback_data=PartnerCB(action="withdraw", asset="MNT"),
        )
    b.button(text=tr("Пригласить друга"), callback_data=CabinetCB(action="invite"))
    b.button(text=tr("В меню"), callback_data=CabinetCB(action="menu"))
    b.adjust(1)
    return b.as_markup()


def _summary_text(summary: service.PartnerSummary) -> str:
    usdt = summary.balances["USDT"]
    text = tr(
        "<b>Партнерская программа</b>\n\nРанг: <b>{v0}</b>\nДоля дохода "
        "владельца: <b>{v1:.0f}%</b>\n\n"
    ).format(v0=summary.rank.level, v1=summary.rank.share * 100)
    if summary.activity is not None:
        activity = summary.activity
        percent = Decimal(activity.active * 100) / activity.total if activity.total else Decimal(0)
        percent = percent.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
        text += tr(
            "Активны в {v0:%m.%Y}: <b>{v1}/{v2} ({v3}%)</b>\nПодтвержденная "
            "ступень: <b>{v4}</b>\n"
        ).format(
            v0=activity.month,
            v1=activity.active,
            v2=activity.total,
            v3=percent,
            v4=activity.qualified_rank.level,
        )
        if summary.protected_until is not None:
            until = summary.protected_until.astimezone(PAYOUT_TZ)
            text += tr("Защита до <b>{v0:%d.%m.%Y %H:%M}</b> (UTC+8)\n").format(v0=until)
        if summary.pending_month is not None:
            text += tr("Проверка за {v0}: ожидаются данные бирж.\n").format(
                v0=summary.pending_month
            )
        text += tr("\n<b>Пожизненный рефбек активных рефералов</b>\n")
    else:
        text += tr("<b>Прямые рефералы по рефбеку за все время</b>\n")
    text += "\n".join(
        tr("От {v0} USDT: <b>{v1}</b>").format(v0=threshold, v1=count)
        for threshold, count in zip(THRESHOLDS, summary.counts, strict=True)
    )
    text += tr(
        "\n\nДоступно: <b>{v0} USDT</b>\nВ выплатах: {v1} USDT\nВыплачено: {v2} USDT"
    ).format(v0=amount(usdt.available), v1=amount(usdt.reserved), v2=amount(usdt.paid))
    mnt = summary.balances["MNT"]
    if mnt.available + mnt.reserved + mnt.paid > 0:
        text += tr(
            "\n\nДоступно: <b>{v0} MNT</b> (~{v1} USDT)\nВ выплатах MNT: ~{v2} "
            "USDT\nВыплачено MNT: ~{v3} USDT"
        ).format(
            v0=amount(mnt.available_native),
            v1=amount(mnt.available),
            v2=amount(mnt.reserved),
            v3=amount(mnt.paid),
        )
    return text


@router.callback_query(CabinetCB.filter(F.action == "partner"))
async def open_partners(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await rank_control.refresh_rank(session, callback.from_user.id)
    summary = await service.summary(session, callback.from_user.id)
    text = _summary_text(summary)
    program = await session.get(PartnerProgram, 1)
    if program is not None:
        text += tr("\n\nНачало учета новых доходов: {v0:%d.%m.%Y} (UTC+8).").format(
            v0=program.starts_on
        )
    await session.commit()
    await state.clear()
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, text, _keyboard(summary))
    await callback.answer()


def _blocked(blocked: WithdrawBlocked) -> str:
    return {
        "paused": tr("Выплаты временно приостановлены администратором."),
        "blocked": tr("Вывод для этого аккаунта недоступен. Обратитесь в поддержку."),
        "pending": tr("У вас уже есть выплата в этой валюте в обработке."),
        "no_wallet": tr("Сначала привяжите кошелек в личном кабинете."),
        "below_min": tr("Минимальная сумма партнерского вывода: 10 USDT или эквивалент в MNT."),
        "mnt_disabled": tr("Вывод MNT временно недоступен."),
    }.get(blocked.reason, tr("Вывод сейчас недоступен."))


@router.callback_query(PartnerCB.filter())
async def partner_callback(
    callback: CallbackQuery, callback_data: PartnerCB, state: FSMContext, session: AsyncSession
) -> None:
    data = callback_data
    keyboard = _back()
    if data.action == "history":
        rows = list(
            (
                await session.scalars(
                    select(PartnerRankReview)
                    .where(PartnerRankReview.user_id == callback.from_user.id)
                    .order_by(PartnerRankReview.month.desc())
                    .limit(12)
                )
            ).all()
        )
        text = tr("<b>История партнерского ранга</b>")
        if not rows:
            text += tr("\n\nМесячных проверок пока нет.")
        for row in rows:
            outcome = (
                tr("ожидаются данные бирж")
                if row.status == "pending"
                else tr("защита ранга")
                if row.reason == "protected"
                else f"{row.previous_rank} → {row.new_rank}"
            )
            text += tr("\n\n<b>{v0:%m.%Y}</b>: {v1}\nАктивны: {v2}/{v3}. Ступень: {v4}.").format(
                v0=row.month, v1=outcome, v2=row.active, v3=row.total, v4=row.qualified_rank
            )
    elif data.action == "ranks":
        lines = []
        for rank in RANKS:
            criteria = tr(" или ").join(
                tr("{v0} с рефбеком {v1}+ USDT").format(v0=count, v1=threshold)
                for threshold, count in zip(THRESHOLDS, rank.counts, strict=True)
                if count is not None
            )
            lines.append(f"<b>{rank.level}: {rank.share * 100:.0f}%</b>, {criteria}")
        text = tr("<b>Партнерские ранги</b>\n\n") + "\n\n".join(lines)
        text += tr(
            "\n\nДо первого ранга: 0%. Чек: подтвержденный рефбек до удержаний "
            "по всем UID приглашенного за все время.\nВознаграждение "
            "финансируется из сбора владельца, рефбек приглашенного не "
            "уменьшается. Прошлые доходы не пересчитываются."
        )
        program = await session.get(PartnerProgram, 1)
        if program is not None and program.version >= 2:
            text += tr(
                "\n\nАктивный реферал: от 10 USDT gross за календарный месяц по всем"
                " UID. В ступени входят только активные рефералы; их чеки остаются"
                " пожизненными.\nПовышение: сразу, с защитой на три календарных "
                "месяца. После защиты неподтвержденный ранг снижается на один "
                "уровень за закрытый месяц.\nДоля активности 30% не понижает "
                "закрытую ступень и не сохраняет незакрытую. Ошибки API не "
                "считаются неактивностью."
            )
    elif data.action == "withdraw":
        result = await withdrawal.prepare(session, callback.from_user.id, data.asset)
        if isinstance(result, WithdrawBlocked):
            text = _blocked(result)
        else:
            token = await withdrawal.issue_token(
                session, callback.from_user.id, result, datetime.now(UTC)
            )
            await session.commit()
            b = InlineKeyboardBuilder()
            b.button(
                text=tr("Подтвердить вывод"),
                callback_data=PartnerCB(action="confirm", asset=result.asset, token=str(token)),
            )
            b.button(text=tr("Назад"), callback_data=CabinetCB(action="partner"))
            b.adjust(1)
            keyboard = b.as_markup()
            text = tr(
                "<b>Вывод партнерского дохода</b>\n\nСумма: <b>{v0} {v1}</b>\nКошелек: {v2}\n"
            ).format(
                v0=amount(result.amount_native), v1=result.asset, v2=escape(result.wallet_masked)
            )
            text += (
                tr("Сеть: Mantle. Выплата после подтверждения администратором.")
                if result.asset == "MNT"
                else tr("Сеть: BEP20. Сетевая комиссия будет удержана при отправке.")
            )
    elif data.action == "confirm":
        try:
            token = uuid.UUID(data.token)
        except ValueError:
            await callback.answer(tr("Кнопка устарела. Начните вывод заново."), show_alert=True)
            return
        payout = await withdrawal.confirm(session, callback.from_user.id, token, datetime.now(UTC))
        await session.commit()
        if isinstance(payout, WithdrawBlocked):
            text = _blocked(payout)
        elif payout is None:
            text = tr("Подтверждение устарело или баланс изменился. Начните вывод заново.")
        else:
            text = tr("Партнерская выплата №{v0} создана: {v1} {v2}.").format(
                v0=payout.id, v1=amount(payout.net_native), v2=payout.asset
            )
            if payout.asset == "MNT":
                text += tr("\nОжидает подтверждения администратором.")
    else:
        await callback.answer(tr("Кнопка устарела."), show_alert=True)
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, text, keyboard)
    await callback.answer()
