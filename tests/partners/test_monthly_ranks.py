from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.invites import InviteAttribution, InviteCode
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import Notification
from bot.db.models.partners import PartnerAccount, PartnerProgram, PartnerRankReview
from bot.partners import rank_control
from bot.partners.activity import ActivitySnapshot, active_counts, load_activity
from bot.payout.expiry import PAYOUT_TZ
from bot.payout.rank_timing import next_month_start

PARENT = 87001
JAN = datetime(2026, 1, 15, 12, tzinfo=UTC)
UTC8 = PAYOUT_TZ


def at(month, day=1):
    return datetime(2026, month, day, 12, tzinfo=UTC)


async def seed_monthly(session, *, version=2):
    session.add(
        PartnerProgram(
            id=1,
            version=version,
            starts_on=date(2026, 1, 1),
            monthly_enabled_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    session.add(User(id=PARENT, role="user", status="active"))
    await session.flush()
    code = InviteCode(user_id=PARENT, code="a1b2c", is_current=True)
    session.add(code)
    await session.flush()
    accounts = []
    # One 10k referee, eight 1k referees, seven 200 referees.
    for index, gross in enumerate([10000] + [1000] * 8 + [200] * 7, start=1):
        user_id = PARENT + index
        session.add(User(id=user_id, role="user", status="active"))
        await session.flush()
        session.add(
            InviteAttribution(
                invitee_id=user_id,
                referrer_id=PARENT,
                code_id=code.id,
                created_at=datetime(2025, 12, 1, tzinfo=UTC),
            )
        )
        account = ExchangeAccount(
            user_id=user_id,
            exchange="gate",
            uid=str(user_id),
            status="archived",
            accrual_since=date(2025, 12, 1),
            created_at=datetime(2025, 12, 1, tzinfo=UTC),
        )
        session.add(account)
        await session.flush()
        accounts.append(account)
        add_gross(session, account, date(2025, 12, 1), Decimal(gross))
    await session.flush()
    return accounts


def add_gross(session, account, day, amount, *, asset="USDT", finalized=True):
    row = RebateAccrual(
        user_id=account.user_id,
        account_id=account.id,
        accrual_date=day,
        asset=asset,
        amount_usdt=amount,
        amount_native=amount,
        commission_usdt=amount * Decimal(".1"),
        net_usdt=amount * Decimal(".9"),
        net_native=amount * Decimal(".9"),
        fee_pct=Decimal(".1"),
        effective_rank=1,
        state="available",
        finalized=finalized,
        available_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    session.add(row)
    return row


async def active_month(session, accounts, month):
    for account in accounts:
        add_gross(session, account, date(2026, month, 1), Decimal(10))
    await session.flush()


async def fill_month(session, account, month, *, missing=None):
    first = date(2026, month, 1)
    day = first
    while day < next_month_start(first):
        if day != missing:
            add_gross(session, account, day, Decimal(0))
        day += timedelta(days=1)
    await session.flush()


@pytest.mark.parametrize(
    "monthly,active",
    [("0", False), ("9.999999999999999999", False), ("10", True), ("10.000000000000000001", True)],
)
def test_activity_uses_normal_monthly_gross_not_carried_rank(monthly, active):
    number, counts = active_counts([(Decimal(10000), Decimal(monthly))])
    assert number == int(active)
    assert counts == ((1, 1, 1, 1) if active else (0, 0, 0, 0))


@pytest.mark.parametrize("active,below", [(0, True), (2, True), (3, False), (4, False)])
def test_thirty_percent_boundary_is_exact(active, below):
    assert ActivitySnapshot(date(2026, 1, 1), 10, active, (0, 0, 0, 0)).below_30_percent is below


def test_zero_referees_no_division_or_false_threshold():
    assert not ActivitySnapshot(date(2026, 1, 1), 0, 0, (0, 0, 0, 0)).below_30_percent


@pytest.mark.parametrize(
    "current,qualified,protected,expected",
    [
        (7, 5, True, 7),
        (7, 5, False, 6),
        (6, 4, False, 5),
        (5, 4, False, 4),
        (4, 5, False, 5),
        (4, 7, True, 7),
        (5, 5, False, 5),
        (1, 0, False, 0),
        (0, 0, False, 0),
    ],
)
def test_grade_moves_one_step_down_and_immediately_up(current, qualified, protected, expected):
    assert rank_control.reviewed_rank(current, qualified, protected) == expected


@pytest.mark.parametrize(
    "start,expected",
    [
        (datetime(2026, 10, 31, 12, tzinfo=UTC), datetime(2027, 1, 31, 12, tzinfo=UTC)),
        (datetime(2026, 11, 30, 12, tzinfo=UTC), datetime(2027, 2, 28, 12, tzinfo=UTC)),
        (datetime(2027, 11, 30, 12, tzinfo=UTC), datetime(2028, 2, 29, 12, tzinfo=UTC)),
        (datetime(2026, 1, 30, 17, tzinfo=UTC), datetime(2026, 4, 29, 17, tzinfo=UTC)),
    ],
)
def test_three_calendar_months_clamp_month_end_in_business_timezone(start, expected):
    assert rank_control.protection_end(start) == expected


def test_naive_protection_time_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        rank_control.protection_end(datetime(2026, 1, 1))


@pytest.mark.asyncio(loop_scope="session")
async def test_qualifying_checks_are_lifetime_but_only_for_active_referees(db_session):
    accounts = await seed_monthly(db_session)
    await active_month(db_session, accounts[1:6], 4)
    add_gross(db_session, accounts[0], date(2026, 5, 1), Decimal(10))
    add_gross(db_session, accounts[1], date(2026, 5, 1), Decimal(10000))
    await db_session.flush()
    snapshot = await load_activity(db_session, PARENT, date(2026, 4, 1), at(5))
    assert snapshot.total == 16 and snapshot.active == 5
    assert snapshot.counts == (5, 5, 5, 0) and snapshot.qualified_rank.level == 5


@pytest.mark.asyncio(loop_scope="session")
async def test_activity_sums_all_uids_and_frozen_usdt_equivalents_even_when_paid(db_session):
    accounts = await seed_monthly(db_session)
    second = ExchangeAccount(
        user_id=accounts[0].user_id,
        exchange="bitget",
        uid="second-uid",
        status="archived",
        accrual_since=date(2025, 12, 1),
        created_at=at(1),
    )
    db_session.add(second)
    await db_session.flush()
    add_gross(db_session, accounts[0], date(2026, 1, 1), Decimal(5), asset="MNT")
    add_gross(db_session, second, date(2026, 1, 1), Decimal(5))
    add_gross(db_session, accounts[1], date(2026, 1, 1), Decimal(10000), finalized=False)
    await db_session.flush()
    rows = list(
        (
            await db_session.scalars(
                select(RebateAccrual).where(
                    RebateAccrual.user_id == accounts[0].user_id,
                    RebateAccrual.accrual_date == date(2026, 1, 1),
                )
            )
        ).all()
    )
    rows[0].state, rows[1].state = "paid", "expired"
    rows[0].amount_native = Decimal(5000)
    await db_session.flush()
    activity = await load_activity(db_session, PARENT, date(2026, 1, 1), JAN)
    assert activity.active == 1 and activity.counts == (1, 1, 1, 1)


@pytest.mark.asyncio(loop_scope="session")
async def test_future_signup_and_open_month_cannot_affect_a_closed_review(db_session):
    accounts = await seed_monthly(db_session)
    attribution = await db_session.get(InviteAttribution, accounts[0].user_id)
    attribution.created_at = at(5)
    await active_month(db_session, accounts[:1], 4)
    snapshot = await load_activity(db_session, PARENT, date(2026, 4, 1), at(5))
    assert snapshot.total == 15 and snapshot.active == 0
    with pytest.raises(ValueError, match="open month"):
        await load_activity(db_session, PARENT, date(2026, 5, 1), at(5), check_coverage=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_protection_then_seven_six_five_four_then_new_five(db_session):
    accounts = await seed_monthly(db_session)
    await active_month(db_session, accounts[:1], 1)
    account = await rank_control.refresh_rank(db_session, PARENT, JAN)
    assert account.current_rank == 7
    original_until = account.protected_until
    assert original_until == at(4, 15)
    # No activity cannot remove the first three calendar months of protection.
    await rank_control.refresh_rank(db_session, PARENT, at(2))
    assert account.current_rank == 7 and account.protected_until == original_until
    await active_month(db_session, accounts[1:6], 4)
    await active_month(db_session, accounts[9:16], 5)
    await active_month(db_session, accounts[9:16], 6)
    await rank_control.refresh_rank(db_session, PARENT, at(5))
    assert account.current_rank == 6
    await rank_control.refresh_rank(db_session, PARENT, at(5, 2))
    assert account.current_rank == 6  # same month, no second downgrade
    await rank_control.refresh_rank(db_session, PARENT, at(6))
    assert account.current_rank == 5
    await rank_control.refresh_rank(db_session, PARENT, at(7))
    assert account.current_rank == 4
    await active_month(db_session, accounts[1:6], 7)
    await rank_control.refresh_rank(db_session, PARENT, at(7, 2))
    assert account.current_rank == 5 and account.protected_until == at(10, 2)
    await rank_control.refresh_rank(db_session, PARENT, at(7, 3))
    assert account.protected_until == at(10, 2)
    rows = list(
        (
            await db_session.scalars(select(PartnerRankReview).order_by(PartnerRankReview.month))
        ).all()
    )
    assert [r.new_rank for r in rows] == [7, 7, 7, 6, 5, 4]


@pytest.mark.asyncio(loop_scope="session")
async def test_closed_step_preserved_even_below_thirty_percent(db_session):
    accounts = await seed_monthly(db_session)
    await active_month(db_session, accounts[:1], 1)
    await rank_control.refresh_rank(db_session, PARENT, JAN)
    await active_month(db_session, accounts[:1], 4)
    snapshot = await load_activity(db_session, PARENT, date(2026, 4, 1), at(5))
    assert snapshot.below_30_percent and snapshot.qualified_rank.level == 7
    account = await rank_control.refresh_rank(db_session, PARENT, at(5))
    assert account.current_rank == 7


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_api_days_wait_then_apply_once_when_data_arrives(db_session):
    accounts = await seed_monthly(db_session)
    account = PartnerAccount(
        user_id=PARENT,
        current_rank=7,
        promoted_at=at(1),
        protected_until=at(4),
        first_qualified_month=date(2026, 1, 1),
        last_review_month=date(2026, 3, 1),
    )
    db_session.add(account)
    accounts[0].status = "active"
    missing = date(2026, 4, 12)
    await fill_month(db_session, accounts[0], 4, missing=missing)
    await active_month(db_session, accounts[1:6], 4)
    await rank_control.refresh_rank(db_session, PARENT, at(5))
    assert account.current_rank == 7 and account.last_review_month == date(2026, 3, 1)
    pending = await db_session.get(PartnerRankReview, (PARENT, date(2026, 4, 1)))
    assert pending.status == "pending" and pending.details["missing_accounts"] == [accounts[0].id]
    await rank_control.refresh_rank(db_session, PARENT, at(5))
    correction = add_gross(db_session, accounts[0], missing, Decimal(0))
    correction.correction_key, correction.fee_pct = "monthly-proof-test", None
    unfinished = add_gross(
        db_session, accounts[0], missing, Decimal(0), asset="MNT", finalized=False
    )
    await db_session.flush()
    await rank_control.refresh_rank(db_session, PARENT, at(5))
    assert pending.status == "pending" and account.current_rank == 7
    add_gross(db_session, accounts[0], missing, Decimal(0))
    await db_session.flush()
    await rank_control.refresh_rank(db_session, PARENT, at(5))
    assert pending.status == "pending"  # A partial asset-day is still unknown.
    unfinished.finalized = True
    await db_session.flush()
    await rank_control.refresh_rank(db_session, PARENT, at(5))
    await rank_control.refresh_rank(db_session, PARENT, at(5))
    assert account.current_rank == 6 and pending.status == "applied"
    assert await db_session.scalar(select(func.count()).select_from(PartnerRankReview)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 1
