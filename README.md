# Commission Rebate Bot

## Current Production Application

The current application is a public Telegram rebate cabinet, not the original
single-owner calculator described in the historical sections below. It has
separate bot, accrual and payout-executor processes, PostgreSQL/Alembic,
support requests, invitations, partner revenue sharing, and eight UI languages.
`ADMIN_TELEGRAM_IDS` controls administrative access; ordinary users can register.

- Runtime source: `bot/`; schema: `alembic/`; regression coverage: `tests/`.
- Business rules: [PAYOUT_BUSINESS_LOGIC.md](PAYOUT_BUSINESS_LOGIC.md).
- Database: [DB_SCHEMA.md](DB_SCHEMA.md).
- User features: [USER_INVITES.md](USER_INVITES.md),
  [REFERRAL_REVENUE_SHARE.md](REFERRAL_REVENUE_SHARE.md),
  [SUPPORT_CASES.md](SUPPORT_CASES.md).
- UI languages: Russian, English, Ukrainian, Simplified Chinese, Turkish, Hindi,
  Thai and Vietnamese; change language with `/language`.
- Tests use disposable PostgreSQL containers and require a working Docker daemon.
  Install with `pip install -e ".[dev]"`, then run `pytest`.

This repository snapshot restores source previously deployed outside GitHub.
See [production-sync.md](docs/production-sync.md) for comparison results, validation
limits and deployment prerequisites. Do not use an automatic full-stack rollout
as part of merely importing the source: verify the actual checkout, environment,
migration order and rollback procedure first.

## Historical Calculator Documentation

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

### Rotating affiliate credentials through Telegram

The optional `/api_keys` owner flow validates a complete replacement credential
set with a read-only exchange request, encrypts it with Fernet, and sends only the
encrypted tokens to an isolated updater over a Unix socket. The updater atomically
writes `/etc/rebate-bot/exchange-keys.env` and recreates only an explicit service
allowlist. The bot has neither the Docker socket nor write access to that file.

Only IDs in `USER_TELEGRAM_IDS` can use this command. It works only in a private
chat, and the credentials message must be deleted successfully before it is parsed.
There is deliberately no TOTP/PIN, so the owner's Telegram account and active
sessions become part of the security boundary. Telegram is still a third-party
transport; use affiliate/read-only, IP-bound keys and never submit a withdrawal
key through this flow.

One-time host preparation:

```bash
sudo install -d -o root -g root -m 0700 /etc/rebate-bot
sudo install -o root -g root -m 0600 /dev/null /etc/rebate-bot/exchange-keys.env
# Set MASTER_KEY, CREDENTIAL_UPDATE_ENABLED=true and the CREDENTIAL_* values in .env.
docker compose --profile credential-updates up -d --build
```

The dedicated file may start empty. Existing exchange values in `.env` remain the
fallback; a successfully rotated value in `exchange-keys.env` takes precedence.
No plaintext backup is created. On a service recreation failure, the updater keeps
the previous bytes only in memory, restores them atomically, and retries recreation.

## 🔐 API key security checklist

- [ ] Each exchange key has **affiliate/read scope only**.
- [ ] **No trade permission. No withdraw permission.** Ever.
- [ ] **Bybit:** the "Affiliate" permission only, master UID.
- [ ] Bind keys to the **VPS IP** wherever the exchange supports IP whitelisting.
- [ ] Secrets live only in the runtime environment — never in code or git.
      `.gitignore` already excludes `.env`, `*.key`, `secrets/`.
- [ ] Keep the host `.env` at `chmod 600`, owned by the service user.

## Payout circuit (V1, optional)

A second, opt-in circuit (`ENABLE_PAYOUT=true`) turns the bot into a rebate-payout
intake + monthly calculator (design: [PAYOUT_ARCHITECTURE.md](PAYOUT_ARCHITECTURE.md)).
**V1 moves no money** — it collects claims, computes the month, and prepares a batch
for the owner, who pays out manually.

- **Access opens up** so referral users can submit claims; owner functions stay
  gated to the whitelist. (With `ENABLE_PAYOUT=false` the bot is whitelist-only, exactly
  as before.)
- **Users** set one active BEP20 wallet and submit claims (exchange + UID) during the
  window (27th → end of month). One UID claimed by two Telegram users → conflict, held
  for owner review.
- **On the 1st** (APScheduler, `PAYOUT_TIMEZONE`) the bot freezes claims, calls the
  affiliate adapters, builds a draft batch (USDT only, ≥ `PAYOUT_MIN_USDT`, rounded
  **down** to `PAYOUT_AMOUNT_PRECISION`), and sends the owner a summary.
- **Owner commands:** `/run_calc [YYYY-MM]`, `/summary`, `/batch`, `/conflicts`,
  `/accept <ex> <uid> <tg_id>`, `/reject <ex> <uid>`, `/approve [YYYY-MM]`.

Required when enabled (see [.env.example](.env.example)): `DATABASE_URL` (async
PostgreSQL), `WALLET_PEPPER` (address-hash pepper), `MASTER_KEY` (wallet encryption),
plus `PAYOUT_*` policy knobs and the `POSTGRES_*` credentials for compose.

Data lives in PostgreSQL; manage the schema with Alembic:

```bash
alembic upgrade head        # apply migrations (DATABASE_URL from env)
```

Wallet addresses are stored encrypted (Fernet) with a separate HMAC hash for
duplicate detection; the full address is shown only on the user's confirmation
screen and is never logged.

### V2 — executing withdrawals

V2 adds the payout-executor: it turns an approved batch into on-chain USDT (BEP20)
withdrawals on MEXC. The approval chain is, by design, deliberate:

`/approve` → `/authorize` (starts a timelock) → `/execute` (after the timelock) →
`/reconcile`. `/cancel_payout` aborts during the timelock.

- **Default `PAYOUT_EXECUTOR_MODE=dry_run`** — nothing is sent until you set `live`.
  The MEXC withdrawal API in `MexcWithdrawalProvider` is implemented against the
  verified spot-v3 docs (withdraw/apply, withdraw/history, account, config/getall;
  network code `BEP20(BSC)`; `withdrawOrderId` idempotency). Before trusting LIVE at
  volume, do one small real withdrawal and confirm the full path end-to-end.
- **Pre-flight**: before sending, the executor confirms via `config/getall` that
  USDT/BEP20 withdrawals are enabled and skips any item below the network minimum.
- **Idempotency**: each item carries a stable key; an item with a recorded
  withdrawal id/txid is never sent twice, and a re-run resumes a partial batch.
- **Balance check** before sending; **`PAYOUT_MAX_TOTAL_USDT`** caps a batch.
- **High-risk secret**: `WITHDRAWAL_MEXC_API_KEY/SECRET` is a *separate*
  withdraw-only key on a dedicated MEXC (sub)account — never the affiliate key.
  Prefer a Docker/host secret file, IP + address whitelist, and daily limits.

> ⚠️ **Security note.** PAYOUT_ARCHITECTURE.md recommends running the executor as a
> *separate* process so a bot compromise can't drain funds. By project decision the
> executor currently runs **inside the bot process**; the `bot/payout/executor/`
> package is kept self-contained so it can be split out later. The withdrawal key is
> loaded lazily and only in LIVE mode. Understand this trade-off before going LIVE.

## Docker

`docker compose` brings up Postgres (private network, no published ports), runs
`alembic upgrade head` once via a `migrate` service, then starts the bot:

```bash
docker compose up -d --build
docker compose logs -f
```

When credential rotation is enabled, start the updater profile too:

```bash
docker compose --profile credential-updates up -d --build
```

The container runs **non-root**, read-only filesystem, `cap_drop: [ALL]`, and
`no-new-privileges`. No ports are published (outbound long polling only).

## CI / CD (GitHub Actions)

Two workflows live in [.github/workflows/](.github/workflows/):

- **CI** ([ci.yml](.github/workflows/ci.yml)) — on every push and PR: `ruff check`,
  `ruff format --check`, `mypy bot`, `pytest`.
- **Deploy** ([deploy.yml](.github/workflows/deploy.yml)) — runs **only after CI
  succeeds on `main`/`master`**. SSHes into the VPS, hard-resets to the pushed
  commit, and runs `docker compose --profile credential-updates up -d --build`.

### One-time VPS prep

```bash
git clone <this-repo> /opt/commission-rebate-bot
cd /opt/commission-rebate-bot
cp .env.example .env && chmod 600 .env   # fill in real values; stays on the VPS, never in git
sudo install -d -o root -g root -m 0700 /etc/rebate-bot
sudo install -o root -g root -m 0600 /dev/null /etc/rebate-bot/exchange-keys.env
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
