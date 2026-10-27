from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.credentials import (
    CredentialFlow,
    on_cancel,
    on_credentials,
    on_non_text_credentials,
)
from bot.security.credential_rotation import RotationReceipt


class _RotationService:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    async def rotate(self, bundle, *, verification_uid: str, actor_id: int):
        self.events.append("rotate")
        self.calls += 1
        assert verification_uid == "12345678"
        assert actor_id == 111
        assert bundle.api_key.get_secret_value() == "new-key"
        return RotationReceipt(request_id="request-id", masked_api_key="****-key")


async def test_credentials_message_is_deleted_before_rotation() -> None:
    events: list[str] = []
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(
        text="new-key\nnew-secret",
        from_user=SimpleNamespace(id=111),
        delete=AsyncMock(side_effect=lambda: events.append("delete")),
        answer=AsyncMock(return_value=status),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "credential_exchange": "gate",
                "credential_verification_uid": "12345678",
            }
        ),
        set_state=AsyncMock(),
        clear=AsyncMock(),
    )
    service = _RotationService(events)

    await on_credentials(
        cast(Message, message),
        cast(FSMContext, state),
        service,  # type: ignore[arg-type]
    )

    assert events == ["delete", "rotate"]
    assert service.calls == 1
    state.clear.assert_awaited_once()
    status.edit_text.assert_awaited_once()
    assert "new-secret" not in status.edit_text.await_args.args[0]


async def test_delete_failure_aborts_before_parsing_or_rotation() -> None:
    message = SimpleNamespace(
        text="new-key\nnew-secret",
        from_user=SimpleNamespace(id=111),
        delete=AsyncMock(side_effect=RuntimeError("delete failed")),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())
    service = _RotationService([])

    await on_credentials(
        cast(Message, message),
        cast(FSMContext, state),
        service,  # type: ignore[arg-type]
    )

    assert service.calls == 0
    state.clear.assert_awaited_once()


async def test_non_text_credential_message_is_also_deleted() -> None:
    message = SimpleNamespace(delete=AsyncMock(), answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())

    await on_non_text_credentials(
        cast(Message, message),
        cast(FSMContext, state),
    )

    message.delete.assert_awaited_once()
    message.answer.assert_awaited_once()


async def test_cancel_closes_prompt_and_clears_credential_state(monkeypatch) -> None:
    delete = AsyncMock()
    monkeypatch.setattr(Message, "delete", delete)
    prompt = Message.model_construct()
    callback = SimpleNamespace(message=prompt, answer=AsyncMock())
    state = SimpleNamespace(
        get_state=AsyncMock(return_value=CredentialFlow.entering_credentials.state),
        clear=AsyncMock(),
    )

    await on_cancel(
        cast(CallbackQuery, callback),
        cast(FSMContext, state),
    )

    state.clear.assert_awaited_once()
    callback.answer.assert_awaited_once_with()
    delete.assert_awaited_once_with()


async def test_stale_cancel_does_not_clear_another_flow(monkeypatch) -> None:
    delete = AsyncMock()
    monkeypatch.setattr(Message, "delete", delete)
    prompt = Message.model_construct()
    callback = SimpleNamespace(message=prompt, answer=AsyncMock())
    state = SimpleNamespace(
        get_state=AsyncMock(return_value="PayoutFlow:entering_wallet"),
        clear=AsyncMock(),
    )

    await on_cancel(
        cast(CallbackQuery, callback),
        cast(FSMContext, state),
    )

    state.clear.assert_not_awaited()
    callback.answer.assert_awaited_once_with()
    delete.assert_awaited_once_with()
