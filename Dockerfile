# syntax=docker/dockerfile:1

# ---- Builder: install deps into an isolated venv ----
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN python -m venv "$VIRTUAL_ENV"

# Project metadata (pyproject references README.md) + source, then install.
COPY pyproject.toml README.md ./
COPY bot ./bot
RUN pip install --upgrade pip && pip install .

# ---- Runtime: minimal, non-root ----
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Dedicated unprivileged user. Stable IDs are enforced by the credential
# updater's Unix peer check; do not change them independently.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home /app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app bot ./bot
# Migrations run in-container (`alembic upgrade head`) — ship the config + versions.
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app alembic ./alembic

USER app

# Default process is the Telegram bot (long polling — no inbound ports). The
# accrual-worker and payout-executor run as separate services with their own commands
# (see docker-compose.yml): `python -m bot.accrual` and `python -m bot.executor`.
CMD ["python", "-m", "bot.main"]
