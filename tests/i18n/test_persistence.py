from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters.command import CommandObject
from sqlalchemy import func, select

from bot.botui import menu, repository
from bot.db.models.identity import User
from bot.db.models.invites import InviteAttribution
from bot.i18n import current_locale, use_locale
from bot.i18n.middleware import LocaleMiddleware
from bot.i18n.router import LanguageCB, select_language
from bot.invites import service as invites
from tests.botui.test_menu import _ctx

pytestmark = pytest.mark.asyncio(loop_scope="session")


def callback(user_id):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), message=None, answer=AsyncMock())


async def test_setting_language_is_owned_persistent_and_does_not_reparent(db_session):
    owner = await repository.upsert_user(db_session, 818001)
    other = await repository.upsert_user(db_session, 818002)
    cb = callback(owner.id)
    with use_locale("ru"):
        await select_language(cb, LanguageCB(code="zh"), _ctx(owner.id), db_session)
    await db_session.refresh(owner)
    await db_session.refresh(other)
    assert owner.locale == "zh" and other.locale == "ru"
    assert await db_session.scalar(select(func.count()).select_from(InviteAttribution)) == 0
    assert cb.answer.await_args.args[0] == "语言已保存。"


async def test_language_selection_does_not_create_unknown_user(db_session):
    cb = callback(818003)
    await select_language(cb, LanguageCB(code="en"), _ctx(818003), db_session)
    assert await db_session.get(User, 818003) is None
    cb.answer.assert_awaited_once()
    assert "/start" in cb.answer.await_args.args[0]


async def test_invalid_language_cannot_be_written(db_session):
    user = await repository.upsert_user(db_session, 818004)
    cb = callback(user.id)
    await select_language(cb, LanguageCB(code="../../en"), _ctx(user.id), db_session)
    await db_session.refresh(user)
    assert user.locale == "ru"


async def test_first_start_attributes_referral_before_persisting_language(db_session, monkeypatch):
    await repository.upsert_user(db_session, 818010)
    code = await invites.current_code(db_session, 818010)
    payload = invites.PREFIX + code.code
    state = _ctx(818011)
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=818011, username=None),
        chat=SimpleNamespace(type="private"),
        answer=AsyncMock(),
    )
    msg.answer.return_value = SimpleNamespace(chat=SimpleNamespace(id=818011), message_id=55)
    monkeypatch.setattr(menu, "_show_mnt_button", AsyncMock(return_value=False))
    with use_locale("en"):
        await menu.open_cabinet(
            msg,
            state,
            db_session,
            frozenset(),
            CommandObject(command="start", args=payload),
        )
    user = await db_session.get(User, 818011)
    attribution = await db_session.get(InviteAttribution, 818011)
    assert user.locale == "en"
    assert attribution.referrer_id == 818010
    assert "Invitation recorded" in msg.answer.await_args.args[0]
    assert "My account" in msg.answer.await_args.args[0]
    # Existing Telegram user must not become another referral after language changes.
    with use_locale("hi"):
        await menu.open_cabinet(
            msg,
            state,
            db_session,
            frozenset(),
            CommandObject(command="start", args=payload),
        )
    await db_session.refresh(user)
    assert user.locale == "en"
    assert await db_session.scalar(select(func.count()).select_from(InviteAttribution)) == 1


async def test_middleware_unknown_user_is_read_only_and_manual_choice_wins(db_session):
    middleware = LocaleMiddleware(frozenset())

    async def handler(event, data):
        assert current_locale() == "en"

    data = {
        "session": db_session,
        "event_from_user": SimpleNamespace(id=818020, language_code="de"),
    }
    await middleware(handler, SimpleNamespace(), data)
    assert await db_session.get(User, 818020) is None
    user = await repository.upsert_user(db_session, 818020)
    user.locale = "en"
    await db_session.flush()
    data["event_from_user"].language_code = "tr"
    await middleware(handler, SimpleNamespace(), data)
    assert current_locale() == "ru"


async def test_admin_support_uses_russian_without_overwriting_user_choice(db_session):
    user = await repository.upsert_user(db_session, 818030)
    user.locale = "vi"
    await db_session.flush()

    async def handler(event, data):
        assert current_locale() == "ru"

    handler.__module__ = "bot.support.router"
    data = {
        "session": db_session,
        "event_from_user": SimpleNamespace(id=user.id, language_code="vi"),
        "handler": SimpleNamespace(callback=handler),
    }
    await LocaleMiddleware(frozenset({user.id}))(handler, SimpleNamespace(), data)
    assert user.locale == "vi"


async def test_admin_cancellation_does_not_query_locale_or_require_a_database(monkeypatch):
    from bot.i18n import middleware as locale_module

    lookup = AsyncMock(side_effect=RuntimeError("Database unavailable"))
    monkeypatch.setattr(locale_module, "saved_locale", lookup)

    async def handler(event, data):
        assert current_locale() == "ru"
        return "cancelled"

    handler.__module__ = "bot.handlers.credentials"
    data = {
        "session": object(),
        "event_from_user": SimpleNamespace(id=818040, language_code="hi"),
        "handler": SimpleNamespace(callback=handler),
    }
    result = await LocaleMiddleware(frozenset({818040}))(handler, SimpleNamespace(), data)
    assert result == "cancelled"
    lookup.assert_not_awaited()
