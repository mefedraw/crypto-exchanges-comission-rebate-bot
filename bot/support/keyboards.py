"""Typed callbacks for case creation, review, and conversations."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.admin.keyboards import AdminCB
from bot.botui.keyboards import CabinetCB
from bot.config import SPEC_BY_CODE
from bot.db.models.identity import ExchangeAccount
from bot.db.models.support import SupportCase, SupportMessage
from bot.i18n import gettext as tr
from bot.payout.expiry import PAYOUT_TZ
from bot.support import statuses


class CaseCB(CallbackData, prefix="case"):
    action: str
    key: str = "0"
    token: str = "-"


def menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("Создать новое обращение"), callback_data=CaseCB(action="open"))
    b.button(text=tr("Мои обращения"), callback_data=CaseCB(action="list"))
    b.button(text=tr("В меню"), callback_data=CabinetCB(action="menu"))
    b.adjust(1)
    return b.as_markup()


def back(*, staff: bool = False, closed: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text=tr("Обращения") if staff else tr("Мои обращения"),
        callback_data=CaseCB(
            action="admin" if staff else "list", token=statuses.CLOSED if closed else "-"
        ),
    )
    b.button(
        text=tr("В админку") if staff else tr("Техническая поддержка"),
        callback_data=AdminCB(action="menu") if staff else CaseCB(action="menu"),
    )
    b.adjust(1)
    return b.as_markup()


def exchanges(accounts: Sequence[ExchangeAccount], token: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code in sorted({a.exchange for a in accounts}):
        spec = SPEC_BY_CODE.get(code)
        b.button(
            text=spec.label if spec else code,
            callback_data=CaseCB(action="exchange", key=code, token=token),
        )
    b.adjust(2)
    b.row(*cancel().inline_keyboard[0])
    return b.as_markup()


def uids(accounts: Sequence[ExchangeAccount], token: str, page: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for account in accounts[page * 10 : (page + 1) * 10]:
        b.button(
            text=account.uid,
            callback_data=CaseCB(action="account", key=str(account.id), token=token),
        )
    b.adjust(1)
    nav = []
    if page:
        nav.append((tr("Назад"), page - 1))
    if len(accounts) > (page + 1) * 10:
        nav.append((tr("Далее"), page + 1))
    for title, target in nav:
        b.button(text=title, callback_data=CaseCB(action="uidpage", key=str(target), token=token))
    b.row(*cancel().inline_keyboard[0])
    return b.as_markup()


def cancel() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("Отмена"), callback_data=CaseCB(action="cancel"))
    return b.as_markup()


def preview(token: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("Отправить"), callback_data=CaseCB(action="send", token=token))
    b.button(text=tr("Изменить текст"), callback_data=CaseCB(action="edit", token=token))
    b.button(text=tr("Отмена"), callback_data=CaseCB(action="cancel"))
    b.adjust(1)
    return b.as_markup()


def case_actions(case: SupportCase, *, staff: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if case.closed_at is None:
        if staff and case.status == statuses.SENT:
            b.button(
                text=tr("Взять в работу"), callback_data=CaseCB(action="take", key=str(case.id))
            )
        b.button(
            text=tr("Ответить")
            if staff or case.status == statuses.REQUIRES_REPLY
            else tr("Дополнить"),
            callback_data=CaseCB(action="reply", key=str(case.id)),
        )
        b.button(text=tr("Закрыть кейс"), callback_data=CaseCB(action="closeask", key=str(case.id)))
    b.button(
        text=tr("История сообщений"),
        callback_data=CaseCB(action="thread", key=str(case.id), token=str(0)),
    )
    b.adjust(1)
    b.attach(
        InlineKeyboardBuilder.from_markup(back(staff=staff, closed=case.closed_at is not None))
    )
    return b.as_markup()


def notification(case_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("Открыть кейс"), callback_data=CaseCB(action="view", key=str(case_id)))
    return b.as_markup()


def close_confirmation(case_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("Закрыть кейс"), callback_data=CaseCB(action="close", key=str(case_id)))
    b.button(text=tr("Назад"), callback_data=CaseCB(action="view", key=str(case_id)))
    b.adjust(1)
    return b.as_markup()


def case_list(
    items: Sequence[SupportCase], page: int, *, staff: bool, closed: bool = False
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if staff:
        b.button(text=tr("Активные"), callback_data=CaseCB(action="admin"))
        b.button(text=tr("Закрытые"), callback_data=CaseCB(action="admin", token=statuses.CLOSED))
    for case in items[:10]:
        b.button(
            text=f"№{case.id} · {case.exchange} · {statuses.label(case.status)}",
            callback_data=CaseCB(action="view", key=str(case.id)),
        )
    action = "admin" if staff else "list"
    token = statuses.CLOSED if staff and closed else "-"
    if page:
        b.button(
            text=tr("Назад"), callback_data=CaseCB(action=action, key=str(page - 1), token=token)
        )
    if len(items) > 10:
        b.button(
            text=tr("Далее"), callback_data=CaseCB(action=action, key=str(page + 1), token=token)
        )
    if not staff:
        b.button(text=tr("Создать новое обращение"), callback_data=CaseCB(action="open"))
    b.button(
        text=tr("В админку") if staff else tr("Техническая поддержка"),
        callback_data=AdminCB(action="menu") if staff else CaseCB(action="menu"),
    )
    if staff:
        b.adjust(2, 1)
    else:
        b.adjust(1)
    return b.as_markup()


def thread(items: Sequence[SupportMessage], case_id: int, page: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item in items[:10]:
        author = tr("Поддержка") if item.is_staff else tr("Пользователь")
        stamp = item.created_at.astimezone(PAYOUT_TZ).strftime("%d.%m %H:%M")
        b.button(
            text=f"{stamp} · {author}", callback_data=CaseCB(action="message", key=str(item.id))
        )
    b.adjust(1)
    if page:
        b.button(
            text=tr("Новее"),
            callback_data=CaseCB(action="thread", key=str(case_id), token=str(page - 1)),
        )
    if len(items) > 10:
        b.button(
            text=tr("Ранее"),
            callback_data=CaseCB(action="thread", key=str(case_id), token=str(page + 1)),
        )
    b.button(text=tr("К кейсу"), callback_data=CaseCB(action="view", key=str(case_id)))
    return b.as_markup()
