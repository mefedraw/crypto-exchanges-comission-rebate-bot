"""Stable support workflow values shared by persistence and presentation."""

from bot.i18n import gettext as tr

SENT = "sent"
OPEN = "open"
REQUIRES_REPLY = "requires_reply"
CLOSED = "closed"

LABELS = {
    SENT: "Отправлено",
    OPEN: "Открыт кейс",
    REQUIRES_REPLY: "Требуется ответ",
    CLOSED: "Кейс закрыт",
}


def label(status: str) -> str:
    return tr(LABELS[status])
