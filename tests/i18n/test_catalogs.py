from __future__ import annotations

import ast
import asyncio
import re
from collections import Counter
from decimal import Decimal
from html import unescape
from pathlib import Path
from string import Formatter
from types import SimpleNamespace

import pytest

from bot.botui.keyboards import main_menu, referral_menu_keyboard
from bot.botui.referrals import referral_detail_text
from bot.botui.referrals_catalog import REFERRALS
from bot.botui.service import WithdrawBlocked
from bot.botui.withdraw import _blocked_text, _preview_text
from bot.db.models.ops import Notification
from bot.i18n import LANGUAGES, current_locale, engine, normalize_locale, use_locale
from bot.i18n import gettext as tr
from bot.i18n.router import keyboard
from bot.notifications import poller, repository
from bot.support import rendering, statuses


def fields(text):
    return Counter(
        (field, spec, conversion)
        for _, field, spec, conversion in Formatter().parse(text)
        if field is not None
    )


def test_every_static_gettext_call_has_a_catalog_entry():
    root = Path(__file__).resolve().parents[2] / "bot"
    catalog = engine.locales["ru"]._catalog
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                assert node.args[0].value in catalog, (path, node.lineno)


@pytest.mark.parametrize("locale", LANGUAGES)
def test_catalog_is_complete_and_preserves_formatting_numbers_and_html(locale):
    source = engine.locales["ru"]._catalog
    catalog = engine.locales[locale]._catalog
    assert set(catalog) == set(source)
    assert len(catalog) == 228
    for key, translated in catalog.items():
        if not key:
            continue
        assert translated
        assert fields(key) == fields(translated), key
        assert Counter(re.findall(r"</?[^>]+>", key)) == Counter(
            re.findall(r"</?[^>]+>", translated)
        ), key
        literal = "".join(part for part, *_ in Formatter().parse(key))
        other = "".join(part for part, *_ in Formatter().parse(translated))
        assert Counter(re.findall(r"\d+", literal)) == Counter(re.findall(r"\d+", other)), key


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("zh-Hans-CN", "zh"),
        ("zh_Hant", "zh"),
        ("uk-UA", "uk"),
        ("tr-TR", "tr"),
        ("hi-IN", "hi"),
        ("th", "th"),
        ("vi-VN", "vi"),
        ("ru-RU", "ru"),
        ("en-US", "en"),
        ("de", "en"),
        (None, "en"),
        ("../../ru", "en"),
        ("", "en"),
    ],
)
def test_locale_normalization_is_allowlisted(raw, expected):
    assert normalize_locale(raw) == expected


@pytest.mark.parametrize("locale", LANGUAGES)
@pytest.mark.parametrize("show_mnt", [False, True])
def test_menu_buttons_keep_callbacks_icons_and_fit_limits(locale, show_mnt):
    with use_locale(locale):
        markup = main_menu(show_mnt=show_mnt).model_dump(exclude_none=True)
        rows = markup["inline_keyboard"]
        assert [len(row) for row in rows] == [2, 2, 2 if show_mnt else 1, 1, 1, 1]
        assert rows[-3][0]["callback_data"] == "case:menu:0:-"
        assert rows[-3][0]["icon_custom_emoji_id"] == "5404494366603522339"
        assert rows[-2][0]["callback_data"] == "cab:invite"
        assert rows[-2][0]["icon_custom_emoji_id"] == "5404830391959858439"
        assert rows[-1][0]["callback_data"] == "cab:language"
        for row in rows:
            for button in row:
                assert 1 <= len(button["text"]) <= 64
                assert len(button["callback_data"].encode()) <= 64
        languages = keyboard().inline_keyboard
        assert [len(row) for row in languages] == [2, 2, 2, 2, 1]
        assert sum(b.text.startswith("✓ ") for row in languages for b in row) == 1


@pytest.mark.parametrize("locale", LANGUAGES)
def test_referral_urls_codes_terms_and_pending_status_are_not_changed(locale):
    with use_locale(locale):
        markup = referral_menu_keyboard()
        assert len(markup.inline_keyboard) == 12
        for entry in REFERRALS:
            text = unescape(referral_detail_text(entry.slug))
            assert entry.url in text
            assert f"<code>{entry.ref_code}</code>" in text
            assert entry.icon_custom_emoji_id in text
            if entry.pending:
                assert "<s>" in text


@pytest.mark.parametrize("locale", LANGUAGES)
def test_payout_amounts_units_and_network_warnings_are_preserved(locale):
    result = SimpleNamespace(
        asset="MNT", net_native=Decimal("12.345"), net=Decimal("6.175"), wallet_masked="0x1234…abcd"
    )
    with use_locale(locale):
        preview = _preview_text(result, None)
        assert "12.35 MNT" in preview and "6.18 USDT" in preview
        assert "0x1234…abcd" in preview
        assert tr("⚠️ Суммы — начисленные биржами данные, не гарантия к выплате.") in preview
        paid = poller.format_message(
            Notification(
                kind=repository.PAYOUT_PAID,
                recipient_chat_id=1,
                payload={"asset": "MNT", "net_native": "12.345", "txhash": "0xabc"},
            ),
            56,
        )
        assert "12.34 MNT" in paid and "12.35 MNT" not in paid
        assert "https://bscscan.com/tx/0xabc" in paid
        assert str(Decimal("10.00")) in _blocked_text(WithdrawBlocked("below_min"), None)


@pytest.mark.parametrize("locale", LANGUAGES)
def test_support_body_is_escaped_but_never_translated(locale):
    body = "Свяжитесь со мной <b>secret</b> हिन्दी 中文"
    with use_locale(locale):
        result = rendering.request(
            exchange="gate", uid="12345678", user_id=123, username=None, body=body
        )
        assert body in unescape(result)
        assert "&lt;b&gt;secret&lt;/b&gt;" in result
        assert statuses.label(statuses.SENT) == tr("Отправлено")
        assert statuses.label(statuses.REQUIRES_REPLY) == tr("Требуется ответ")


@pytest.mark.asyncio(loop_scope="session")
async def test_locale_is_task_local_and_restored_after_failure():
    async def worker(locale):
        with use_locale(locale):
            before = tr("Техническая поддержка")
            await asyncio.sleep(0)
            assert current_locale() == locale
            assert tr("Техническая поддержка") == before
            return before

    values = await asyncio.gather(*(worker(locale) for locale in LANGUAGES))
    assert len(set(values)) == len(LANGUAGES)
    assert current_locale() == "ru"
    with pytest.raises(RuntimeError), use_locale("zh"):
        raise RuntimeError("test")
    assert current_locale() == "ru"
