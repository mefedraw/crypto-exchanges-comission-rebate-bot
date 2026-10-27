from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.security import access
from bot.security.access import BlockGuardMiddleware
from bot.security.ratelimit import RateLimitMiddleware


class _Recorder:
    """Stand-in handler that records whether it was called."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, event, data):
        self.calls += 1
        return "handled"


async def test_block_guard_allows_normal_user(monkeypatch: pytest.MonkeyPatch):
    """Open access: an ordinary, non-blocked user reaches the handler."""
    monkeypatch.setattr(access, "get_user_status", AsyncMock(return_value=None))
    mw = BlockGuardMiddleware(frozenset())
    handler = _Recorder()
    data = {"event_from_user": SimpleNamespace(id=5), "session": object()}
    result = await mw(handler, SimpleNamespace(), data)
    assert result == "handled"
    assert handler.calls == 1


async def test_block_guard_rejects_blocked_user(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(access, "get_user_status", AsyncMock(return_value="blocked"))
    mw = BlockGuardMiddleware(frozenset())
    handler = _Recorder()
    data = {"event_from_user": SimpleNamespace(id=5), "session": object()}
    result = await mw(handler, SimpleNamespace(), data)
    assert result is None
    assert handler.calls == 0


async def test_block_guard_exempts_staff_without_db_lookup(monkeypatch: pytest.MonkeyPatch):
    """Admins/developer are never turned away — and short-circuit before any status query."""
    status = AsyncMock(return_value="blocked")
    monkeypatch.setattr(access, "get_user_status", status)
    mw = BlockGuardMiddleware(frozenset({7}))
    handler = _Recorder()
    data = {"event_from_user": SimpleNamespace(id=7), "session": object()}
    result = await mw(handler, SimpleNamespace(), data)
    assert result == "handled"
    assert handler.calls == 1
    status.assert_not_called()


async def test_block_guard_passes_through_without_user():
    """Non-user updates (no event_from_user) are not gated."""
    mw = BlockGuardMiddleware(frozenset())
    handler = _Recorder()
    result = await mw(handler, SimpleNamespace(), {})
    assert result == "handled"
    assert handler.calls == 1


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


async def test_rate_limit_never_drops_credential_flow_events():
    mw = RateLimitMiddleware(
        min_interval=100.0,
        bypass_states=frozenset(
            {
                "CredentialFlow:choosing_exchange",
                "CredentialFlow:entering_uid",
                "CredentialFlow:entering_credentials",
                "CredentialFlow:applying",
            }
        ),
    )
    handler = _Recorder()
    for raw_state in (
        "CredentialFlow:choosing_exchange",
        "CredentialFlow:entering_uid",
        "CredentialFlow:entering_credentials",
        "CredentialFlow:applying",
    ):
        data = {
            "event_from_user": SimpleNamespace(id=1),
            "raw_state": raw_state,
        }
        await mw(handler, SimpleNamespace(), data)
        await mw(handler, SimpleNamespace(), data)

    assert handler.calls == 8
