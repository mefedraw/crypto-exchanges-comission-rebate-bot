"""Local, task-isolated UI translations. Financial values never pass through gettext."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aiogram.utils.i18n import I18n

LANGUAGES = {
    "ru": "Русский",
    "en": "English",
    "uk": "Українська",
    "zh": "简体中文",
    "tr": "Türkçe",
    "hi": "हिन्दी",
    "th": "ไทย",
    "vi": "Tiếng Việt",
}

engine = I18n(path=Path(__file__).with_name("locales"), default_locale="ru")
_recent: OrderedDict[int, str] = OrderedDict()


def normalize_locale(code: str | None) -> str:
    base = (code or "").replace("_", "-").lower().split("-", 1)[0]
    return base if base in LANGUAGES else "en"


def gettext(message: str) -> str:
    return engine.gettext(message)


def current_locale() -> str:
    return engine.current_locale


@contextmanager
def use_locale(locale: str) -> Iterator[None]:
    with engine.use_locale(normalize_locale(locale)):
        yield


def remember_locale(user_id: int, locale: str) -> None:
    _recent[user_id] = normalize_locale(locale)
    _recent.move_to_end(user_id)
    if len(_recent) > 4096:
        _recent.popitem(last=False)


def recent_locale(user_id: int, telegram_code: str | None) -> str:
    return _recent.get(user_id, normalize_locale(telegram_code))
