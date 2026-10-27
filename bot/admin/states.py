# bot/admin/states.py
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminFlow(StatesGroup):
    waiting_commission = State()
    waiting_rate = State()
    waiting_user_status = State()
    waiting_reassign = State()
    waiting_block_uid = State()
