from __future__ import annotations

from datetime import date

from bot.config import Settings


def _base_env(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("USER_TELEGRAM_IDS", "1")
    monkeypatch.setenv("DEVELOPER_TELEGRAM_ID", "2")


def test_rebate_epoch_defaults_to_program_start(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("REBATE_EPOCH", raising=False)
    assert Settings().rebate_epoch == date(2026, 7, 10)  # type: ignore[call-arg]


def test_rebate_epoch_parses_iso_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("REBATE_EPOCH", "2026-08-01")
    assert Settings().rebate_epoch == date(2026, 8, 1)  # type: ignore[call-arg]
