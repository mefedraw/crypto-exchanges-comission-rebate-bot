"""Import every model module so Base.metadata is fully populated."""

from bot.db.models import (
    identity,  # noqa: F401
    invites,  # noqa: F401
    ledger,  # noqa: F401
    ops,  # noqa: F401
    partners,  # noqa: F401
    payouts,  # noqa: F401
    support,  # noqa: F401
)
