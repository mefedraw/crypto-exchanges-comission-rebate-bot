from __future__ import annotations

import json

import pytest
from aiogram import Bot

from bot.botui.keyboards import main_menu


@pytest.mark.parametrize("show_mnt", [False, True])
def test_main_menu_icons_preserve_actions_labels_and_layout(show_mnt):
    markup = main_menu(show_mnt=show_mnt).model_dump(exclude_none=True)
    rows = markup["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 2, 2 if show_mnt else 1, 1, 1, 1]
    assert rows[-3] == [
        {
            "text": "Техническая поддержка",
            "callback_data": "case:menu:0:-",
            "icon_custom_emoji_id": "5404494366603522339",
        }
    ]
    assert rows[-2] == [
        {
            "text": "Пригласить друга",
            "callback_data": "cab:invite",
            "icon_custom_emoji_id": "5404830391959858439",
        }
    ]
    assert rows[-1][0]["callback_data"] == "cab:language"
    assert [b["callback_data"] for row in rows[:-3] for b in row] == [
        "cab:wallet",
        "cab:uids",
        "cab:stats",
        "cab:ref",
        "wd:USDT",
        *(["wd:MNT"] if show_mnt else []),
    ]
    assert all("icon_custom_emoji_id" not in b for row in rows[:-3] for b in row)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("show_mnt", [False, True])
async def test_pinned_aiogram_serializes_both_main_menu_icons(show_mnt):
    bot = Bot("123:abc")
    try:
        payload = bot.session.prepare_value(main_menu(show_mnt=show_mnt), bot=bot, files={})
        rows = json.loads(payload)["inline_keyboard"]
        assert rows[-3][0]["icon_custom_emoji_id"] == "5404494366603522339"
        assert rows[-2][0]["icon_custom_emoji_id"] == "5404830391959858439"
        assert rows[-3][0]["callback_data"] == "case:menu:0:-"
        assert rows[-2][0]["callback_data"] == "cab:invite"
    finally:
        await bot.session.close()
