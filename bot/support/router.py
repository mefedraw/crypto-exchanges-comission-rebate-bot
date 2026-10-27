"""Cabinet case workflow and staff replies, with an explicit send preview."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.botui import panel
from bot.botui import repository as cabinet_repo
from bot.db.models.support import SupportMessage
from bot.i18n import gettext as tr
from bot.support import keyboards as kb
from bot.support import rendering, service, statuses
from bot.support.keyboards import CaseCB

router = Router(name="support-cases")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


class CaseFlow(StatesGroup):
    exchange = State()
    account = State()
    text = State()
    preview = State()


def _number(raw: str, *, zero: bool = False) -> int:
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
        raise service.CaseError(tr("Кейс не найден или кнопка устарела."))
    value = int(raw)
    if value < (0 if zero else 1) or value > (10000 if zero else 10**18 - 1):
        raise service.CaseError(tr("Кейс не найден или кнопка устарела."))
    return value


async def _draft(state: FSMContext, token: str | None = None) -> dict[str, Any]:
    data = await state.get_data()
    stamp = data.get("support_created")
    if not isinstance(stamp, str) or (token is not None and data.get("support_token") != token):
        raise service.CaseError(tr("Черновик устарел. Откройте кейс заново."))
    if datetime.now(UTC) - datetime.fromisoformat(stamp) > timedelta(minutes=30):
        raise service.CaseError(tr("Черновик устарел. Откройте кейс заново."))
    return data


async def _show(
    callback: CallbackQuery, state: FSMContext, text: str, keyboard: InlineKeyboardMarkup
) -> None:
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, text, keyboard)


async def _open(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, admin_ids: frozenset[int]
) -> None:
    await state.clear()
    user_id = callback.from_user.id
    gross = await service.lifetime_gross(session, user_id)
    if gross < service.MIN_GROSS:
        text = tr(
            "<b>Создание обращения пока недоступно</b>\n\nДля создания обращения"
            " нужно накопить не менее <b>100 USDT рефбека</b> за все время по "
            "всем вашим аккаунтам.\n\nСейчас учтено: <b>{v0} USDT</b> до "
            "удержаний. Уже выведенные суммы тоже учитываются."
        ).format(v0=rendering.amount(gross))
        await _show(callback, state, text, kb.back())
        return
    if not admin_ids:
        raise service.CaseError(tr("Обращения временно недоступны. Пожалуйста, попробуйте позже."))
    await service.check_creation_limits(session, user_id, datetime.now(UTC))
    accounts = await service.accounts(session, user_id)
    if not accounts:
        await _show(
            callback,
            state,
            tr("У вас нет активных UID. Сначала привяжите аккаунт биржи в личном кабинете."),
            kb.back(),
        )
        return
    token = secrets.token_hex(12)
    await state.set_state(CaseFlow.exchange)
    await state.update_data(
        support_token=token, support_mode="new", support_created=datetime.now(UTC).isoformat()
    )
    await _show(
        callback,
        state,
        tr("<b>Новое обращение</b>\n\nВыберите биржу:"),
        kb.exchanges(accounts, token),
    )


async def _view(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    case_id: int,
    admin_ids: frozenset[int],
) -> None:
    case = await service.get_case(session, case_id, callback.from_user.id, admin_ids)
    items = await service.messages(session, case_id)
    latest = items[-1] if items else None
    body = (tr("Поддержка:\n") if latest and latest.is_staff else tr("Пользователь:\n")) + (
        latest.body if latest else ""
    )
    text = tr("Статус: <b>{v0}</b>\n\n").format(
        v0=statuses.label(case.status)
    ) + rendering.case_request(case, body)
    await state.clear()
    await _show(
        callback, state, text, kb.case_actions(case, staff=callback.from_user.id in admin_ids)
    )


async def _dispatch(
    callback: CallbackQuery,
    data: CaseCB,
    state: FSMContext,
    session: AsyncSession,
    admin_ids: frozenset[int],
) -> None:
    actor_id = callback.from_user.id
    action = data.action
    if action == "menu":
        await state.clear()
        await _show(callback, state, tr("<b>Техническая поддержка</b>"), kb.menu())
    elif action == "open":
        await _open(callback, state, session, admin_ids)
    elif action == "cancel":
        context = await state.get_data()
        current = await state.get_state()
        if (
            current
            and current.startswith("CaseFlow:")
            and isinstance(callback.message, Message)
            and context.get("panel_msg") == callback.message.message_id
        ):
            await state.clear()
        if isinstance(callback.message, Message):
            await panel.delete_message(callback.message)
    elif action in {"exchange", "uidpage"}:
        context = await _draft(state, data.token)
        if await state.get_state() not in {CaseFlow.exchange.state, CaseFlow.account.state}:
            raise service.CaseError(tr("Кнопка устарела. Откройте кейс заново."))
        exchange = data.key if action == "exchange" else context.get("support_exchange")
        accounts = [a for a in await service.accounts(session, actor_id) if a.exchange == exchange]
        if not accounts:
            raise service.CaseError(tr("У вас нет активных UID на этой бирже."))
        page = _number(data.key, zero=True) if action == "uidpage" else 0
        if page * 10 >= len(accounts):
            raise service.CaseError(tr("Страница не найдена."))
        await state.set_state(CaseFlow.account)
        await state.update_data(support_exchange=exchange)
        await _show(
            callback, state, tr("<b>Выберите UID аккаунта</b>"), kb.uids(accounts, data.token, page)
        )
    elif action == "account":
        context = await _draft(state, data.token)
        if await state.get_state() != CaseFlow.account.state:
            raise service.CaseError(tr("Кнопка устарела. Откройте кейс заново."))
        account = await service.owned_account(session, actor_id, _number(data.key))
        if account.exchange != context.get("support_exchange"):
            raise service.CaseError(tr("Выберите UID выбранной биржи."))
        await state.set_state(CaseFlow.text)
        await state.update_data(support_account=account.id)
        await _show(
            callback,
            state,
            tr(
                "<b>Кейс: {v0}, UID {v1}</b>\n\nОпишите ваш вопрос одним текстовым "
                "сообщением (до 1200 символов).\nНе указывайте API-ключи, пароли и "
                "seed-фразы."
            ).format(v0=escape(account.exchange), v1=escape(account.uid)),
            kb.cancel(),
        )
    elif action == "edit":
        await _draft(state, data.token)
        if await state.get_state() != CaseFlow.preview.state:
            raise service.CaseError(tr("Кнопка устарела. Откройте кейс заново."))
        await state.set_state(CaseFlow.text)
        await _show(
            callback,
            state,
            tr("Отправьте исправленный текст сообщения (до 1200 символов)."),
            kb.cancel(),
        )
    elif action == "send":
        context = await _draft(state, data.token)
        if await state.get_state() != CaseFlow.preview.state:
            raise service.CaseError(tr("Этот черновик уже отправлен или отменен."))
        body = service.validate_body(context.get("support_body"))
        if context.get("support_mode") == "new":
            receipt = await service.open_case(
                session,
                user_id=actor_id,
                account_id=int(context["support_account"]),
                body=body,
                token=data.token,
                admin_ids=admin_ids,
                now=datetime.now(UTC),
            )
        else:
            receipt = await service.reply(
                session,
                case_id=int(context["support_case"]),
                actor_id=actor_id,
                body=body,
                token=data.token,
                admin_ids=admin_ids,
                now=datetime.now(UTC),
            )
        # Commit the case and its outbox before acknowledging acceptance in Telegram.
        await session.commit()
        await state.clear()
        text = tr("<b>Обращение №{v0}</b>\nСтатус: <b>{v1}</b>\n\nСообщение принято.").format(
            v0=receipt.case.id, v1=statuses.label(receipt.case.status)
        )
        await _show(
            callback, state, text, kb.case_actions(receipt.case, staff=actor_id in admin_ids)
        )
    elif action in {"list", "admin"}:
        staff = action == "admin"
        if staff and actor_id not in admin_ids:
            raise service.CaseError(tr("Обращения недоступны."))
        closed = staff and data.token == statuses.CLOSED
        if staff and data.token not in {"-", statuses.CLOSED}:
            raise service.CaseError(tr("Кнопка устарела. Откройте обращения заново."))
        page = _number(data.key, zero=True)
        items = await service.list_cases(session, actor_id, staff=staff, page=page, closed=closed)
        await state.clear()
        text = (
            tr("<b>Обращения</b>\n\n") + (tr("Закрытые") if closed else tr("Активные"))
            if staff
            else tr("<b>Мои обращения</b>")
        )
        if not items:
            text += tr("\n\nОбращений пока нет.")
        await _show(callback, state, text, kb.case_list(items, page, staff=staff, closed=closed))
    elif action == "take":
        receipt = await service.take_case(
            session,
            case_id=_number(data.key),
            actor_id=actor_id,
            admin_ids=admin_ids,
            now=datetime.now(UTC),
        )
        await session.commit()
        await _view(callback, state, session, receipt.case.id, admin_ids)
    elif action == "view":
        await _view(callback, state, session, _number(data.key), admin_ids)
    elif action == "reply":
        case = await service.get_case(session, _number(data.key), actor_id, admin_ids)
        if case.closed_at:
            raise service.CaseError(tr("Этот кейс уже закрыт."))
        await state.clear()
        await state.set_state(CaseFlow.text)
        await state.update_data(
            support_mode="reply",
            support_case=case.id,
            support_token=secrets.token_hex(12),
            support_created=datetime.now(UTC).isoformat(),
        )
        await _show(
            callback,
            state,
            tr("<b>Кейс №{v0}</b>\n\nОтправьте текст сообщения (до 1200 символов).").format(
                v0=case.id
            ),
            kb.cancel(),
        )
    elif action == "closeask":
        case = await service.get_case(session, _number(data.key), actor_id, admin_ids)
        await state.clear()
        await _show(
            callback,
            state,
            tr("Закрыть кейс №{v0}?").format(v0=case.id),
            kb.close_confirmation(case.id),
        )
    elif action == "close":
        receipt = await service.close_case(
            session,
            case_id=_number(data.key),
            actor_id=actor_id,
            admin_ids=admin_ids,
            now=datetime.now(UTC),
        )
        await session.commit()
        await _view(callback, state, session, receipt.case.id, admin_ids)
    elif action == "thread":
        case_id = _number(data.key)
        await service.get_case(session, case_id, actor_id, admin_ids)
        page = _number(data.token, zero=True)
        thread_items = list(
            (
                await session.scalars(
                    select(SupportMessage)
                    .where(SupportMessage.case_id == case_id)
                    .order_by(SupportMessage.id.desc())
                    .offset(page * 10)
                    .limit(11)
                )
            ).all()
        )
        await state.clear()
        await _show(
            callback,
            state,
            tr("<b>История кейса №{v0}</b>").format(v0=case_id),
            kb.thread(thread_items, case_id, page),
        )
    elif action == "message":
        item = await session.get(SupportMessage, _number(data.key))
        if item is None:
            raise service.CaseError(tr("Сообщение недоступно."))
        case = await service.get_case(session, item.case_id, actor_id, admin_ids)
        await state.clear()
        await _show(
            callback,
            state,
            rendering.reply(case.id, item.body, staff=item.is_staff),
            kb.case_actions(case, staff=actor_id in admin_ids),
        )
    else:
        raise service.CaseError(tr("Кнопка устарела. Откройте кейс заново."))


@router.callback_query(CaseCB.filter())
async def case_callback(
    callback: CallbackQuery,
    callback_data: CaseCB,
    state: FSMContext,
    session: AsyncSession,
    admin_ids: frozenset[int],
) -> None:
    try:
        await _dispatch(callback, callback_data, state, session, admin_ids)
    except service.CaseError as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    await callback.answer()


@router.message(CaseFlow.text)
async def case_text(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, admin_ids: frozenset[int]
) -> None:
    if message.from_user is None:
        return
    try:
        context = await _draft(state)
        body = service.validate_body(message.text)
        if context.get("support_mode") == "new":
            account = await service.owned_account(
                session, message.from_user.id, int(context["support_account"])
            )
            user = await cabinet_repo.upsert_user(
                session, message.from_user.id, message.from_user.username
            )
            text = rendering.request(
                exchange=account.exchange,
                uid=account.uid,
                user_id=message.from_user.id,
                username=user.username if user else None,
                body=body,
            )
            title = tr("<b>Предпросмотр обращения для владельца</b>\n\n")
        else:
            case = await service.get_case(
                session, int(context["support_case"]), message.from_user.id, admin_ids
            )
            if case.closed_at:
                raise service.CaseError(tr("Этот кейс уже закрыт."))
            text = rendering.reply(case.id, body, staff=message.from_user.id in admin_ids)
            title = tr("<b>Предпросмотр сообщения</b>\n\n")
        token = secrets.token_hex(12)
        await state.update_data(support_body=body, support_token=token)
        await state.set_state(CaseFlow.preview)
        await panel.delete_message(message)
        await panel.edit_or_answer(bot, state, message, title + text, kb.preview(token))
    except service.CaseError as exc:
        await panel.edit_or_answer(bot, state, message, escape(str(exc)), kb.cancel())
