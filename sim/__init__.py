"""Standalone month-scale simulation harness for the rebate bot.

Runs the REAL accrual worker, botui withdrawal service, admin repository and
payout executor against an isolated throwaway Postgres (testcontainers), with a
controllable clock and fake exchange adapters, so the whole product lifecycle can
be exercised over simulated months without touching production or real funds.

Not part of the pytest suite; run directly:  ``uv run python -m sim.harness``.
"""
