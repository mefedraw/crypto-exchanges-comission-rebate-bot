"""Builds the live set of adapters from configured credentials.

Concrete adapters register themselves here (code -> factory). The registry then
instantiates only those exchanges that have a full credential set, so the bot's
exchange menu always reflects what is actually usable.
"""

from __future__ import annotations

from collections.abc import Callable

from bot.config import EXCHANGE_SPECS, ExchangeCredentials, Settings
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.stub import StubAdapter

# code -> factory(credentials, settings) -> adapter.
# Populated by each adapter module as it is implemented (the adapter stages).
AdapterFactory = Callable[[ExchangeCredentials, Settings], ExchangeAdapter]
_FACTORIES: dict[str, AdapterFactory] = {}


def register(code: str) -> Callable[[AdapterFactory], AdapterFactory]:
    """Decorator for an adapter factory keyed by exchange code."""

    def decorator(factory: AdapterFactory) -> AdapterFactory:
        if code in _FACTORIES:
            raise ValueError(f"Adapter already registered for code {code!r}")
        _FACTORIES[code] = factory
        return factory

    return decorator


def _load_adapter_modules() -> None:
    """Import adapter modules so their ``@register`` decorators run.

    Deferred (called at build time, not import time) to avoid a circular import:
    each adapter module imports :func:`register` from this module.
    """
    from bot.exchanges import (  # noqa: F401  (imported for registration side effects)
        bitget,
        bybit,
        gate,
        kucoin,
        mexc,
        okx,
        weex,
    )


def build_adapters(settings: Settings) -> dict[str, ExchangeAdapter]:
    """Instantiate the live adapter set.

    In DEMO_MODE every exchange is backed by a :class:`StubAdapter`. Otherwise an
    exchange is included only when it has both a full credential set and a
    registered factory.
    """
    if settings.demo_mode:
        return {spec.code: StubAdapter(spec.code, spec.label) for spec in EXCHANGE_SPECS}

    _load_adapter_modules()

    adapters: dict[str, ExchangeAdapter] = {}
    for code, creds in settings.available_exchanges.items():
        factory = _FACTORIES.get(code)
        if factory is not None:
            adapters[code] = factory(creds, settings)
    return adapters
