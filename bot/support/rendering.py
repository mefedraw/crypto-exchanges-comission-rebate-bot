"""HTML-safe, shared preview and notification rendering."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from html import escape

from bot.config import SPEC_BY_CODE
from bot.db.models.support import SupportCase
from bot.i18n import gettext as tr


def amount(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def request(
    *,
    exchange: str,
    uid: str,
    user_id: int,
    username: str | None,
    body: str,
    case_id: int | None = None,
) -> str:
    spec = SPEC_BY_CODE.get(exchange)
    title = (
        tr("Обращение №{v0}").format(v0=case_id) if case_id is not None else tr("Новое обращение")
    )
    contact = f"@{escape(username)}" if username else f"<code>{user_id}</code>"
    return tr(
        "<b>{v0}</b>\nБиржа: {v1}\nUID: <code>{v2}</code>\nПользователь: {v3}"
        "\nTelegram ID: <code>{v4}</code>\n\n{v5}"
    ).format(
        v0=title,
        v1=escape(spec.label if spec else exchange),
        v2=escape(uid),
        v3=contact,
        v4=user_id,
        v5=escape(body),
    )


def case_request(case: SupportCase, body: str) -> str:
    return request(
        exchange=case.exchange,
        uid=case.uid,
        user_id=case.user_id,
        username=case.username,
        body=body,
        case_id=case.id,
    )


def reply(case_id: int, body: str, *, staff: bool) -> str:
    title = tr("Ответ поддержки") if staff else tr("Дополнение пользователя")
    return tr("<b>Обращение №{v0}: {v1}</b>\n\n{v2}").format(v0=case_id, v1=title, v2=escape(body))
