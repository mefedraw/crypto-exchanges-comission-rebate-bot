# bot/botui/states.py
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class WalletFlow(StatesGroup):
    waiting_address = State()


class UidFlow(StatesGroup):
    choosing_exchange = State()
    waiting_uid = State()


class WithdrawFlow(StatesGroup):
    confirming = State()
