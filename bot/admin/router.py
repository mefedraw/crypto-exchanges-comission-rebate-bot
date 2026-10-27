# bot/admin/router.py
"""Admin panel entry point: `/admin` menu + kill-switch toggle.

Release/reject of pending_admin payouts (+ one-time commission reduction) live
here too (Task 4), as do the personal commission rate, UID-dispute resolution,
and user block/unblock controls (Task 5). Kill-switch/blocked enforcement in
the executor and withdraw flow is Task 6.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin import repository as repo
from bot.admin import service
from bot.admin.filters import IsAdmin
from bot.admin.keyboards import (
    AdminCB,
    PayoutActionCB,
    admin_back_keyboard,
    admin_menu,
    payout_action_keyboard,
    user_controls_keyboard,
)
from bot.admin.states import AdminFlow
from bot.botui import panel
from bot.botui import repository as botui_repo
from bot.botui.repository import write_audit
from bot.config import get_settings
from bot.db.models.payouts import Payout
from bot.executor.chain_web3 import (
    Web3ChainClient,
    build_chain_client,
    build_mantle_chain_client,
)
from bot.payout.expiry import PAYOUT_TZ
from bot.payout.rank_timing import month_start, next_month_start
from bot.utils.money import format_2dp

router = Router(name="admin")

_MENU_TEXT = "<b>Админ-панель</b>\n\nВыберите действие:"
_REDUCE_REASON = "Разовое снижение комиссии администратором"
# NOTE: the bot sends with parse_mode=HTML, so any literal angle brackets in a
# user-facing string MUST be HTML entities (&lt;/&gt;) — a raw "<ник>" makes
# Telegram try to parse "ник" as a tag and rejects the whole message.
_RATE_PROMPT = (
    "💬 <b>Персональная ставка</b> — это комиссия бота: сколько <b>процентов</b> бот "
    "удерживает с брутто-рефбека пользователя за месяц (на руки = брутто − комиссия). "
    "Действует «только вниз» — не выше ранговой комиссии "
    "(Манки 1 — 10%, Манки 2 — 7%, Манки 3 — 3%, Манки 4 — 0%).\n\n"
    "Введите: &lt;ник или user_id&gt; &lt;процент 0-100 | clear&gt;\n"
    "Например: <code>@nick 2</code> (2%) или <code>12345 0</code> (без комиссии).\n"
    "Сбросить к ранговой: <code>@nick clear</code>."
)
_USER_STATUS_PROMPT = (
    "Введите: &lt;ник или user_id&gt; active|blocked, " "например @nick blocked или 12345 active."
)
_REASSIGN_PROMPT = (
    "Введите: &lt;биржа&gt; &lt;uid&gt; &lt;ник или user_id&gt;, например bybit U123 @nick."
)
_BLOCK_UID_PROMPT = "Введите: &lt;биржа&gt; &lt;uid&gt;, например bybit U123."


async def _resolve_target(session: AsyncSession, target: str) -> int | None:
    """Resolve an admin-typed target to a user id. Admins only have @nicknames (no DB
    access), so both a numeric user id and a ``@nick``/bare nick are accepted; the
    nick is looked up via the stored username. Returns ``None`` if unresolved.

    A positive all-digit token is a literal id; "0"/non-digits fall through to
    username resolution (Telegram ids are always positive)."""
    if target.isdigit() and int(target) > 0:
        return int(target)
    return await botui_repo.find_user_id_by_username(session, target)


def _pct_str(fraction: Decimal) -> str:
    """Render a stored fraction (e.g. 0.02) as a percent string without trailing
    zeros or exponent notation (e.g. "2", "2.5")."""
    pct = fraction * 100
    text = format(pct, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


async def _panel_text_result(
    bot: Bot,
    state: FSMContext,
    message: Message,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
) -> None:
    """Result of a text-input admin step: drop the user's typed message and edit the
    live panel in place (fall back to a fresh panel), like the user cabinet does."""
    await panel.delete_message(message)
    await panel.edit_or_answer(bot, state, message, text, kb or admin_back_keyboard())


def _format_downline(snapshot: dict[str, Any] | None) -> str:
    """Render the whole-downline monthly rebate block (all referrals, incl. those not
    registered in the bot) for the admin stats, from the worker's saved snapshot."""
    if not snapshot:
        return "\n\n💹 Рефбек со ВСЕЙ базы: ещё не посчитано (обновится в ближайший цикл)."
    total = format_2dp(Decimal(str(snapshot.get("total_usdt", "0"))))
    as_of_dt: datetime | None = None
    try:
        as_of_dt = datetime.fromisoformat(snapshot["as_of"]).astimezone(PAYOUT_TZ)
    except (KeyError, ValueError, TypeError):
        as_of_dt = None
    as_of = as_of_dt.strftime("%d.%m %H:%M") if as_of_dt else "?"
    # Period END is the last CLOSED day actually covered (`through`), NOT the as_of
    # update time — the snapshot deliberately excludes the still-moving current day
    # (closed-days-only, like per-user accrual). Fall back to the as_of date only for
    # older snapshots written before `through` existed.
    through_dt: datetime | None = None
    try:
        through_dt = datetime.fromisoformat(str(snapshot["through"]))
    except (KeyError, ValueError, TypeError):
        through_dt = None
    end_dt = through_dt or as_of_dt
    # Concrete period: 1st of the snapshot's month .. the end date (UTC+8), not just
    # the vague "за месяц".
    month = str(snapshot.get("month") or "")
    if month.count("-") == 1 and end_dt is not None:
        year, mon = month.split("-")
        period = f"с 01.{mon}.{year} по {end_dt:%d.%m.%Y}"
    else:
        period = "за текущий месяц"
    per: dict[str, str] = snapshot.get("per_exchange", {}) or {}
    lines = [
        f"\n\n💹 <b>Рефбек со ВСЕЙ базы</b> ({period}, UTC+8):",
        f"• Всего: <b>{total} USDT</b>  (обновлено {as_of})",
    ]
    if per:
        lines.append(
            "  "
            + " · ".join(f"{code} {format_2dp(Decimal(str(v)))}" for code, v in sorted(per.items()))
        )
    missing = sorted(set((snapshot.get("errors") or []) + (snapshot.get("unsupported") or [])))
    if missing:
        lines.append(f"⚠️ Нет данных: {', '.join(missing)}")
    guarded = sorted(snapshot.get("guarded") or [])
    if guarded:
        lines.append(
            f"⚠️ {', '.join(guarded)}: биржа отдала аномальное значение — "
            "показана медиана последних замеров (сглажен разовый выброс)."
        )
    return "\n".join(lines)


def build_admin_router(admin_ids: frozenset[int]) -> Router:
    """Gate every handler on this router behind ``IsAdmin(admin_ids)`` and return it.

    Call exactly once at wiring time (Task 7), before including the router in the
    dispatcher — the admin-id set is fixed for the process lifetime.
    """
    router.message.filter(IsAdmin(admin_ids))
    router.callback_query.filter(IsAdmin(admin_ids))
    return router


@router.message(Command("admin"))
async def open_admin_menu(message: Message, state: FSMContext) -> None:
    # Fresh entry point: drop any half-finished admin flow, post the menu and remember
    # it as the live panel every later admin action edits in place.
    await state.clear()
    sent = await message.answer(_MENU_TEXT, reply_markup=admin_menu())
    await panel.remember(state, sent)


@router.callback_query(AdminCB.filter(F.action == "menu"))
async def back_to_admin_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, _MENU_TEXT, admin_menu())
    await callback.answer()


@router.callback_query(AdminCB.filter(F.action == "killswitch"))
async def toggle_killswitch(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    admin_id = callback.from_user.id
    paused = await repo.is_payouts_paused(session)
    new_state = not paused
    await repo.set_flag(session, repo.PAYOUTS_PAUSED_KEY, {"paused": new_state}, admin_id)
    await write_audit(
        session,
        actor=str(admin_id),
        action="killswitch_toggle",
        entity_type="system_flag",
        entity_id=repo.PAYOUTS_PAUSED_KEY,
        new_value={"paused": new_state},
    )
    text = "⏸ Выплаты приостановлены." if new_state else "▶️ Выплаты возобновлены."
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, text, admin_back_keyboard())
    await callback.answer()


@router.callback_query(AdminCB.filter(F.action == "stats"))
async def show_stats(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    # Current month in the payout timezone (UTC+8), matching accrual bucketing everywhere.
    today = datetime.now(UTC).astimezone(PAYOUT_TZ).date()
    m_start = month_start(today)
    o = await repo.admin_overview(
        session, month_start=m_start, month_before=next_month_start(m_start)
    )
    # Total actually paid out of the bot wallet, all time — USDT plus any native MNT.
    paid_line = f"{format_2dp(o['paid_usdt'])} USDT"
    if o["paid_mnt_native"] > 0:
        paid_line += (
            f" + {format_2dp(o['paid_mnt_native'])} MNT (~{format_2dp(o['paid_mnt_usdt'])} USDT)"
        )
    text = (
        "📊 <b>Статистика</b>\n\n"
        "<b>Всего</b> (за всё время):\n"
        f"• Заработано (брутто): {format_2dp(o['earned_total'])} USDT\n"
        f"• Рефбек юзерам: {format_2dp(o['rebate_total'])} USDT\n"
        f"• 💰 Профит: <b>{format_2dp(o['profit_total'])} USDT</b>\n"
        f"Партнерам начислено: {format_2dp(o['partner_total'])} USDT\n"
        f"Доход владельца: <b>{format_2dp(o['owner_profit_total'])} USDT</b>\n"
        f"• 💸 Выплачено с кошелька: {paid_line} ({o['paid_count']} выплат)\n\n"
        f"<b>Текущий месяц</b> (с {m_start:%d.%m.%Y} по {today:%d.%m.%Y}, UTC+8):\n"
        f"• Заработано (брутто): {format_2dp(o['earned_month'])} USDT\n"
        f"• Рефбек юзерам: {format_2dp(o['rebate_month'])} USDT\n"
        f"• 💰 Профит: <b>{format_2dp(o['profit_month'])} USDT</b>\n"
        f"Партнерам начислено: {format_2dp(o['partner_month'])} USDT\n"
        f"Доход владельца: <b>{format_2dp(o['owner_profit_month'])} USDT</b>\n\n"
        f"Выплаты: {'⏸ на паузе' if o['paused'] else '▶️ активны'}\n"
        f"Пользователи: {o['users_total']} (заблокировано: {o['users_blocked']})\n"
        f"Активных UID: {o['active_accounts']}\n\n"
        f"Доступно к выводу (net, все юзеры): {format_2dp(o['available_net'])} USDT\n"
        f"Заявок в очереди (reserved): {o['reserved']}\n"
        f"Ждут решения (крупные): {o['pending_admin']}\n"
        f"В процессе отправки: {o['in_flight']}"
    )
    text += _format_downline(o["downline"])
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, text, admin_back_keyboard())
    await callback.answer()


@router.callback_query(AdminCB.filter(F.action == "wallet"))
async def show_wallet_balance(callback: CallbackQuery, state: FSMContext) -> None:
    """On-chain balances of the hot payout wallet — so the admin can see whether there
    is enough USDT/BNB (BSC) and MNT (Mantle) to actually pay out. Read-only RPC calls;
    an RPC failure is surfaced per-chain, never crashes the handler."""
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    settings = get_settings()
    lines = ["💰 <b>Баланс кошелька выплат</b>", ""]
    bsc = build_chain_client(settings)
    if isinstance(bsc, Web3ChainClient):
        lines.append(f"Адрес: <code>{bsc.address}</code>\n")
        try:
            usdt = await bsc.usdt_balance()
            bnb = await bsc.native_balance()
            lines.append(
                f"BSC — USDT: <b>{format_2dp(usdt)}</b>" if usdt is not None else "BSC — USDT: н/д"
            )
            lines.append(f"BSC — BNB (газ): {bnb:.6f}")
        except Exception as exc:  # surface RPC failure, don't crash the panel
            lines.append(f"BSC — ошибка чтения баланса ({type(exc).__name__})")
    else:
        lines.append("BSC/USDT-выплаты не настроены.")
    mantle = build_mantle_chain_client(settings)
    if isinstance(mantle, Web3ChainClient):
        try:
            mnt = await mantle.native_balance()
            lines.append(f"Mantle — MNT (выплаты + газ): <b>{mnt:.6f}</b>")
        except Exception as exc:  # surface RPC failure, don't crash the panel
            lines.append(f"Mantle — ошибка чтения баланса ({type(exc).__name__})")
    else:
        lines.append("Mantle (MNT): выключено.")
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, "\n".join(lines), admin_back_keyboard())
    await callback.answer()


def _payout_summary(payout: Payout) -> str:
    net_line = (
        f"Net: {format_2dp(payout.net_native)} MNT (≈{format_2dp(payout.net_usdt)} USDT)"
        if payout.asset == "MNT"
        else f"Net: {format_2dp(payout.net_usdt)} USDT"
    )
    return (
        f"Заявка #{payout.id} — user {payout.user_id}\n"
        f"Период: {payout.covered_from} — {payout.covered_to}\n"
        f"Актив: {payout.asset}\n"
        f"Gross: {format_2dp(payout.gross_usdt)} USDT\n"
        f"Комиссия: {format_2dp(payout.commission_usdt)} USDT\n"
        f"{net_line}\n"
        f"Кошелёк: {payout.address_masked}"
    )


async def _next_pending_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    """One-at-a-time view: the FIRST pending_admin payout with its action buttons, or a
    "none left" message with a back button. Re-querying the first each time means
    release/reject/reduce just re-render this to advance — no cursor to track."""
    payouts = await repo.list_pending_admin(session)
    if not payouts:
        return "✅ Нет выплат, ожидающих решения.", admin_back_keyboard()
    payout = payouts[0]
    return _payout_summary(payout), payout_action_keyboard(payout.id, payout.asset)


@router.callback_query(AdminCB.filter(F.action == "pending"))
async def show_pending(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    text, kb = await _next_pending_view(session)
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, text, kb)
    await callback.answer()


@router.callback_query(PayoutActionCB.filter(F.action == "release"))
async def release_action(
    callback: CallbackQuery, callback_data: PayoutActionCB, state: FSMContext, session: AsyncSession
) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    admin_id = callback.from_user.id
    pid = callback_data.payout_id
    ok = await repo.release_payout(session, pid, admin_id)
    if ok:
        await write_audit(
            session,
            actor=str(admin_id),
            action="payout_released",
            entity_type="payout",
            entity_id=str(pid),
        )
    # Advance the panel to the next pending payout; confirm the action via a toast.
    text, kb = await _next_pending_view(session)
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, text, kb)
    await callback.answer(f"✅ Заявка #{pid} одобрена." if ok else "⚠️ Уже обработана.")


@router.callback_query(PayoutActionCB.filter(F.action == "reject"))
async def reject_action(
    callback: CallbackQuery, callback_data: PayoutActionCB, state: FSMContext, session: AsyncSession
) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    admin_id = callback.from_user.id
    pid = callback_data.payout_id
    ok = await repo.reject_payout(session, pid)
    if ok:
        await write_audit(
            session,
            actor=str(admin_id),
            action="payout_rejected",
            entity_type="payout",
            entity_id=str(pid),
        )
    text, kb = await _next_pending_view(session)
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, text, kb)
    await callback.answer(f"❌ Заявка #{pid} отклонена." if ok else "⚠️ Уже обработана.")


@router.callback_query(PayoutActionCB.filter(F.action == "reduce"))
async def request_reduce(
    callback: CallbackQuery, callback_data: PayoutActionCB, state: FSMContext
) -> None:
    if callback.from_user is None:  # aiogram types this Optional; callback updates always set it
        await callback.answer()
        return
    await state.update_data(payout_id=callback_data.payout_id)
    await state.set_state(AdminFlow.waiting_commission)
    if isinstance(callback.message, Message):
        await panel.show(
            state,
            callback.message,
            f"Введите новую (сниженную) комиссию в USDT для заявки #{callback_data.payout_id}.",
            admin_back_keyboard(),
        )
    await callback.answer()


@router.message(AdminFlow.waiting_commission, F.text)
async def receive_new_commission(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    if message.from_user is None:  # aiogram types this Optional; message updates always set it
        return
    admin_id = message.from_user.id
    await panel.delete_message(message)  # keep the chat clean: drop the typed number
    data = await state.get_data()
    pid = data.get("payout_id")
    if pid is None:
        await state.clear()
        await panel.edit_or_answer(
            bot,
            state,
            message,
            "Сессия истекла. Начните заново через «💸 Крупные выплаты».",
            admin_back_keyboard(),
        )
        return
    new_commission = service.parse_commission_amount(message.text or "")
    if new_commission is None:
        # Stay in the flow; re-prompt in the panel (state unchanged -> next number retries).
        await panel.edit_or_answer(
            bot,
            state,
            message,
            "⚠️ Введите неотрицательное число, например 12.5.",
            admin_back_keyboard(),
        )
        return
    reduced = await repo.reduce_payout_commission(
        session, pid, new_commission, admin_id, _REDUCE_REASON
    )
    if not reduced:
        await state.clear()
        await panel.edit_or_answer(
            bot,
            state,
            message,
            f"⚠️ Не удалось снизить комиссию заявки #{pid} — сумма выше исходной "
            "или заявка уже не ожидает решения, либо сбор уже распределен партнеру.",
            admin_back_keyboard(),
        )
        return
    await write_audit(
        session,
        actor=str(admin_id),
        action="commission_reduced",
        entity_type="payout",
        entity_id=str(pid),
        new_value={"commission_override_usdt": str(new_commission)},
    )
    await repo.release_payout(session, pid, admin_id)
    await write_audit(
        session,
        actor=str(admin_id),
        action="payout_released",
        entity_type="payout",
        entity_id=str(pid),
    )
    await state.clear()
    # Confirm + advance to the next pending payout in the same panel.
    _, kb = await _next_pending_view(session)
    await panel.edit_or_answer(
        bot,
        state,
        message,
        f"✅ Комиссия заявки #{pid} снижена до {format_2dp(new_commission)} USDT, заявка одобрена.",
        kb,
    )


# ---------------------------------------------------------------------------
# Personal commission rate (B)
# ---------------------------------------------------------------------------


@router.callback_query(AdminCB.filter(F.action == "rate"))
async def request_rate(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.waiting_rate)
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, _RATE_PROMPT, admin_back_keyboard())
    await callback.answer()


@router.message(AdminFlow.waiting_rate, F.text)
async def receive_rate(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    if message.from_user is None:  # aiogram types this Optional; message updates always set it
        return
    admin_id = message.from_user.id
    parsed = service.parse_rate_command(message.text or "")
    if parsed is None:
        # Invalid input: re-prompt in the panel, stay in the flow.
        await _panel_text_result(bot, state, message, f"⚠️ Неверный формат. {_RATE_PROMPT}")
        return
    target, pct = parsed
    user_id = await _resolve_target(session, target)
    if user_id is None:
        await _panel_text_result(
            bot,
            state,
            message,
            f"⚠️ Пользователь {target} не найден — он должен был хотя бы раз написать боту.",
        )
        return
    await state.clear()
    # Current month in the payout timezone (UTC+8), matching accrual bucketing everywhere.
    today = datetime.now(UTC).astimezone(PAYOUT_TZ).date()
    m_start = month_start(today)
    m_before = next_month_start(m_start)
    if pct is None:
        await repo.clear_commission_override(session, user_id)
        recomputed = await repo.recompute_current_month_unpaid(
            session, user_id, None, m_start, m_before
        )
        await write_audit(
            session,
            actor=str(admin_id),
            action="commission_override_cleared",
            entity_type="user",
            entity_id=str(user_id),
            new_value={"recomputed": recomputed},
        )
        await _panel_text_result(
            bot,
            state,
            message,
            f"✅ Персональная ставка пользователя {user_id} сброшена. "
            f"Пересчитано начислений за текущий месяц: {recomputed}.\n"
            "Сборы, уже распределенные партнерам, не изменяются.",
        )
        return
    since = m_start
    await repo.set_commission_override(session, user_id, pct, since)
    recomputed = await repo.recompute_current_month_unpaid(session, user_id, pct, m_start, m_before)
    await write_audit(
        session,
        actor=str(admin_id),
        action="commission_override_set",
        entity_type="user",
        entity_id=str(user_id),
        new_value={"pct": str(pct), "since": since.isoformat(), "recomputed": recomputed},
    )
    await _panel_text_result(
        bot,
        state,
        message,
        f"✅ Ставка {_pct_str(pct)}% для пользователя {user_id} установлена с {since.isoformat()}. "
        f"Пересчитано начислений за текущий месяц: {recomputed}.\n"
        "Сборы, уже распределенные партнерам, не изменяются.",
    )


# ---------------------------------------------------------------------------
# Block/unblock user + UID dispute resolution
# ---------------------------------------------------------------------------


@router.callback_query(AdminCB.filter(F.action == "users"))
async def show_user_controls(callback: CallbackQuery, state: FSMContext) -> None:
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, "Выберите действие:", user_controls_keyboard())
    await callback.answer()


@router.callback_query(AdminCB.filter(F.action == "block_user"))
async def request_user_status(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.waiting_user_status)
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, _USER_STATUS_PROMPT, admin_back_keyboard())
    await callback.answer()


@router.message(AdminFlow.waiting_user_status, F.text)
async def receive_user_status(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    if message.from_user is None:  # aiogram types this Optional; message updates always set it
        return
    admin_id = message.from_user.id
    parsed = service.parse_user_status_command(message.text or "")
    if parsed is None:
        await _panel_text_result(bot, state, message, f"⚠️ Неверный формат. {_USER_STATUS_PROMPT}")
        return
    target, status = parsed
    user_id = await _resolve_target(session, target)
    if user_id is None:
        await _panel_text_result(
            bot,
            state,
            message,
            f"⚠️ Пользователь {target} не найден — он должен был хотя бы раз написать боту.",
        )
        return
    await state.clear()
    await repo.set_user_status(session, user_id, status)
    await write_audit(
        session,
        actor=str(admin_id),
        action="user_status_set",
        entity_type="user",
        entity_id=str(user_id),
        new_value={"status": status},
    )
    await _panel_text_result(bot, state, message, f"✅ Статус пользователя {user_id}: {status}.")


@router.callback_query(AdminCB.filter(F.action == "reassign_uid"))
async def request_reassign(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.waiting_reassign)
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, _REASSIGN_PROMPT, admin_back_keyboard())
    await callback.answer()


@router.message(AdminFlow.waiting_reassign, F.text)
async def receive_reassign(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    if message.from_user is None:  # aiogram types this Optional; message updates always set it
        return
    admin_id = message.from_user.id
    parsed = service.parse_reassign_command(message.text or "")
    if parsed is None:
        await _panel_text_result(bot, state, message, f"⚠️ Неверный формат. {_REASSIGN_PROMPT}")
        return
    exchange, uid, target = parsed
    new_user_id = await _resolve_target(session, target)
    if new_user_id is None:
        await _panel_text_result(
            bot,
            state,
            message,
            f"⚠️ Пользователь {target} не найден — он должен был хотя бы раз написать боту.",
        )
        return
    await state.clear()
    ok = await repo.reassign_uid(session, exchange, uid, new_user_id)
    if ok:
        await write_audit(
            session,
            actor=str(admin_id),
            action="uid_reassigned",
            entity_type="exchange_account",
            entity_id=f"{exchange}:{uid}",
            new_value={"new_user_id": new_user_id},
        )
        await _panel_text_result(
            bot,
            state,
            message,
            f"✅ UID {uid} на {exchange} переназначен пользователю {new_user_id}.",
        )
    else:
        await _panel_text_result(bot, state, message, f"⚠️ Аккаунт {exchange}/{uid} не найден.")


@router.callback_query(AdminCB.filter(F.action == "block_uid"))
async def request_block_uid(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.waiting_block_uid)
    if isinstance(callback.message, Message):
        await panel.show(state, callback.message, _BLOCK_UID_PROMPT, admin_back_keyboard())
    await callback.answer()


@router.message(AdminFlow.waiting_block_uid, F.text)
async def receive_block_uid(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    if message.from_user is None:  # aiogram types this Optional; message updates always set it
        return
    admin_id = message.from_user.id
    parsed = service.parse_block_uid_command(message.text or "")
    if parsed is None:
        await _panel_text_result(bot, state, message, f"⚠️ Неверный формат. {_BLOCK_UID_PROMPT}")
        return
    exchange, uid = parsed
    await state.clear()
    ok = await repo.block_uid(session, exchange, uid)
    if ok:
        await write_audit(
            session,
            actor=str(admin_id),
            action="uid_blocked",
            entity_type="exchange_account",
            entity_id=f"{exchange}:{uid}",
        )
        await _panel_text_result(bot, state, message, f"⛔ UID {uid} на {exchange} заблокирован.")
    else:
        await _panel_text_result(
            bot, state, message, f"⚠️ Активный аккаунт {exchange}/{uid} не найден."
        )
