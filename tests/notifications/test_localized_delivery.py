from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from bot.db.models.identity import User
from bot.i18n import current_locale
from bot.notifications import poller, repository


@pytest.mark.asyncio(loop_scope="session")
async def test_delivery_uses_current_saved_language_without_locale_leak(sessionmaker):
    async with sessionmaker() as session:
        session.add_all([User(id=819100, locale="zh"), User(id=819101, locale="tr")])
        await repository.enqueue(
            session,
            recipient_chat_id=819100,
            kind=repository.PAYOUT_PAID,
            payload={"net_usdt": "12.34"},
        )
        await repository.enqueue(
            session,
            recipient_chat_id=819101,
            kind=repository.PAYOUT_FAILED,
            payload={"net_usdt": "56.78"},
        )
        await session.commit()
    bot = AsyncMock()
    sent = await poller.deliver_pending(
        bot, sessionmaker, chain_id=56, now=lambda: datetime.now(UTC)
    )
    assert sent == 2
    texts = {call.args[0]: call.args[1] for call in bot.send_message.await_args_list}
    assert "收款人已收到资金" in texts[819100] and "12.34 USDT" in texts[819100]
    assert "gönderilemedi" in texts[819101] and "56.78 USDT" in texts[819101]
    assert current_locale() == "ru"
