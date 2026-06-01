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

# Dedicated unprivileged user.
RUN groupadd --system app && useradd --system --gid app --no-create-home --home /app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app bot ./bot

USER app

# Long polling — no inbound ports required.
CMD ["python", "-m", "bot.main"]
