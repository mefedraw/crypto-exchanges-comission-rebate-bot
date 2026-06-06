# Commission Rebate Bot

Single-owner Telegram bot that aggregates **affiliate/rebate commission** across
crypto exchanges by `uid` and date range, to help compute payouts.

> ⚠️ Values are **accrued** commission per the exchange API — not a guaranteed
> "to be paid" amount. Each result carries a disclaimer; for Bybit (and any
> discrepancy) reconcile against the partner web portal.

Supported exchanges (8): **Gate, KuCoin, MEXC, Bitget, BitMart, OKX, Bybit, WEEX**.
(Toobit is not supported — no affiliate API.)

## Stack

Python 3.11+ · aiogram 3 · httpx · pydantic v2 · cryptography (Fernet) · structlog.
Deploys as Docker (long polling, no inbound ports).

## Quick start (local)

```bash
uv sync                       # or: poetry install
cp .env.example .env          # then fill in values (see below)
python -m bot.main
```

## Configuration

All configuration comes from the environment (see [.env.example](.env.example)).

- `BOT_TOKEN` — Telegram bot token.
- `USER_TELEGRAM_IDS` — owners who use the bot, comma-separated (one or more).
- `DEVELOPER_TELEGRAM_ID` — receives failure alerts (unhandled errors, API-key
  problems) and may also use the bot. May be one of `USER_TELEGRAM_IDS`.
  The whitelist is all user IDs plus the developer; every other update is
  silently ignored.
- `MASTER_KEY` — optional Fernet key. When set, exchange credentials are treated
  as encrypted tokens and decrypted in memory at runtime.
- `<EXCHANGE>_API_KEY` / `_API_SECRET` / `_API_PASSPHRASE` — per exchange. BitMart
  futures affiliate endpoints currently need only `BITMART_API_KEY`. Leave an
  exchange blank to skip it; only sufficiently credentialed exchanges are registered.

### Preparing encrypted secrets

```bash
# 1) Generate a master key (store it in your secret manager / runtime env only):
python -m bot.security.secrets keygen

# 2) Encrypt each exchange secret (reads stdin, prints a Fernet token):
MASTER_KEY=<key> python -m bot.security.secrets encrypt
```

Put the master key in `MASTER_KEY` and the Fernet tokens in the per-exchange vars.

## 🔐 API key security checklist

- [ ] Each exchange key has **affiliate/read scope only**.
- [ ] **No trade permission. No withdraw permission.** Ever.
- [ ] **Bybit:** the "Affiliate" permission only, master UID.
- [ ] Bind keys to the **VPS IP** wherever the exchange supports IP whitelisting.
- [ ] Secrets live only in the runtime environment — never in code or git.
      `.gitignore` already excludes `.env`, `*.key`, `secrets/`.
- [ ] Keep the host `.env` at `chmod 600`, owned by the service user.

## Docker

```bash
docker compose up -d --build
docker compose logs -f
```

The container runs **non-root**, read-only filesystem, `cap_drop: [ALL]`, and
`no-new-privileges`. No ports are published (outbound long polling only).

## CI / CD (GitHub Actions)

Two workflows live in [.github/workflows/](.github/workflows/):

- **CI** ([ci.yml](.github/workflows/ci.yml)) — on every push and PR: `ruff check`,
  `ruff format --check`, `mypy bot`, `pytest`.
- **Deploy** ([deploy.yml](.github/workflows/deploy.yml)) — runs **only after CI
  succeeds on `main`/`master`**. SSHes into the VPS, hard-resets to the pushed
  commit, and runs `docker compose up -d --build`.

### One-time VPS prep

```bash
git clone <this-repo> /opt/commission-rebate-bot
cd /opt/commission-rebate-bot
cp .env.example .env && chmod 600 .env   # fill in real values; stays on the VPS, never in git
# ensure docker + compose v2 are installed and the deploy user can run docker
```

### Required GitHub Actions secrets

Set under *Settings → Secrets and variables → Actions*:

| Secret | Purpose |
|---|---|
| `VPS_SSH_HOST` | VPS hostname or IP |
| `VPS_SSH_USER` | SSH user (must be able to run `docker`) |
| `VPS_SSH_KEY` | Private SSH key (PEM) for that user |
| `VPS_SSH_PORT` | SSH port (optional; defaults to 22) |
| `VPS_PROJECT_DIR` | Absolute path to the cloned repo, e.g. `/opt/commission-rebate-bot` |

Because deploy does `git reset --hard origin/<branch>`, the VPS checkout must have
its `origin` pointing at this repository. The bot's `.env` is **not** in git — it
lives on the VPS only.

> Alternative (not configured here): build an image, push to GHCR, and have the
> VPS pull it. The SSH pull-and-rebuild flow above matches the docker-compose
> setup in this repo and keeps secrets off CI.

## Development

```bash
ruff check . && ruff format --check .
mypy bot
pytest
pip-audit
```

Always run `ruff check`, `mypy bot`, and `pytest` before committing.

## Project layout

```
bot/
  main.py            # entry point, long polling
  config.py          # pydantic Settings; derives available exchanges
  logging.py         # structlog config with secret redaction
  security/          # access (whitelist), secrets (Fernet), ratelimit
  handlers/          # start, FSM flow, error handler
  keyboards.py
  exchanges/
    base.py          # ExchangeAdapter ABC + CommissionResult/CommissionLine
    signing.py       # HMAC signing helpers
    <exchange>.py    # one adapter per exchange
    registry.py      # registers exchanges that have full credentials
  utils/
    dates.py         # date parsing + window slicing
    money.py         # Decimal summation and formatting
tests/
```

See [TZ_commission_bot.md](TZ_COM~1.MD) and [CLAUDE.md](CLAUDE.md) for the full
spec and contributor guide.
