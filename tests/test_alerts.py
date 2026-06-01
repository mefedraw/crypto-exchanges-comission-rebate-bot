from __future__ import annotations

from typing import Any

from bot.alerts import Alerter


class _FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self._fail = fail

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        if self._fail:
            raise RuntimeError("network down")
        self.sent.append((chat_id, text))


async def test_send_goes_to_developer():
    bot = _FakeBot()
    await Alerter(bot, 999).send("Бот запущен", "детали")
    assert bot.sent[0][0] == 999
    assert "Бот запущен" in bot.sent[0][1]


async def test_alert_exception_includes_type_message_and_context():
    bot = _FakeBot()
    try:
        raise ValueError("boom")
    except ValueError as exc:
        await Alerter(bot, 999).alert_exception("Сбой", exc, context="exchange: gate")

    text = bot.sent[0][1]
    assert "ValueError" in text
    assert "boom" in text
    assert "exchange: gate" in text


async def test_send_failure_is_swallowed():
    bot = _FakeBot(fail=True)
    # Must not raise even though send_message fails.
    await Alerter(bot, 999).send("anything")
    assert bot.sent == []


async def test_message_is_length_capped():
    bot = _FakeBot()
    try:
        raise RuntimeError("x" * 10000)
    except RuntimeError as exc:
        await Alerter(bot, 1).alert_exception("big", exc)
    assert len(bot.sent[0][1]) <= 4096
