from __future__ import annotations

from types import SimpleNamespace

from bot.security.access import WhitelistMiddleware
from bot.security.ratelimit import RateLimitMiddleware


class _Recorder:
    """Stand-in handler that records whether it was called."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, event, data):
        self.calls += 1
        return "handled"


async def test_whitelist_allows_known_user():
    mw = WhitelistMiddleware(frozenset({111}))
    handler = _Recorder()
    data = {"event_from_user": SimpleNamespace(id=111)}
    result = await mw(handler, SimpleNamespace(), data)
    assert result == "handled"
    assert handler.calls == 1


async def test_whitelist_drops_unknown_user():
    mw = WhitelistMiddleware(frozenset({111}))
    handler = _Recorder()
    data = {"event_from_user": SimpleNamespace(id=999)}
    result = await mw(handler, SimpleNamespace(), data)
    assert result is None
    assert handler.calls == 0


async def test_whitelist_drops_when_no_user():
    mw = WhitelistMiddleware(frozenset({111}))
    handler = _Recorder()
    result = await mw(handler, SimpleNamespace(), {})
    assert result is None
    assert handler.calls == 0


async def test_rate_limit_throttles_second_call():
    # SimpleNamespace event is neither Message nor CallbackQuery -> no notify call.
    mw = RateLimitMiddleware(min_interval=100.0)
    handler = _Recorder()
    data = {"event_from_user": SimpleNamespace(id=1)}

    first = await mw(handler, SimpleNamespace(), data)
    second = await mw(handler, SimpleNamespace(), data)

    assert first == "handled"
    assert second is None
    assert handler.calls == 1


async def test_rate_limit_independent_users():
    mw = RateLimitMiddleware(min_interval=100.0)
    handler = _Recorder()
    await mw(handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id=1)})
    await mw(handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id=2)})
    assert handler.calls == 2
