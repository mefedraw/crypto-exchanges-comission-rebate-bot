# Log shipping to Grafana Cloud via Fluent Bit — Design

Date: 2026-07-18
Status: Approved (pending spec review)

## Problem

The owner wants to browse the bot's logs — operational, business/audit, errors, and
status — in a browser, with authentication, **without weakening the server's security
posture** (today: zero inbound ports, long-polling bot, hardened containers). Docker's
`json-file` driver rotates at 10 MB × 3 per service, so old logs disappear; the owner
wants history retained.

## Constraints that shaped the design

- **Server is tiny and already under pressure:** 1 GB RAM (~336 MB free, ~930 MB swap
  already in use), 1 CPU, 15 GB disk at 80% (3 GB free), already running 6 containers
  including the live payout stack. A local Grafana+Loki stack (~400–500 MB RAM + growing
  disk) would push the box into heavy swap/OOM and could destabilize the payout bot —
  unacceptable on a wallet server.
- **Security first:** this host signs on-chain payouts. Any new component must not add an
  inbound port, must not touch the payout key/DB in a mutating way, and must minimize
  attack surface and supply-chain risk.

## Decision

Ship logs **off the server** to **Grafana Cloud** (free tier: ~50 GB/mo ingest, 14-day
retention, Loki backend, hosted UI + auth + 2FA + alerting) using a **Fluent Bit** agent
that reads Docker's JSON log files read-only and pushes over **outbound TLS only**.

This *strengthens* the server posture: no inbound port is added (not even a VPN), there
is no UI on the server to attack, and the agent is outbound-only. The trade-off — logs
leave the box to a third party — is bounded by (a) our existing rule that secrets are
never logged and keys are masked, and (b) a redaction pass in the agent (below).

### Why Fluent Bit (vs Alloy / Vector)

Footprint is the deciding factor on a 1 GB box. Fluent Bit is ~10–30 MB RAM (C), tails
Docker JSON logs, redacts via filters, and has a native `loki` output that pushes to
Grafana Cloud. Grafana Alloy (~100 MB) and Vector (~40–80 MB) are heavier with no benefit
here.

## Architecture / data flow

```
/var/lib/docker/containers/*/*-json.log   (bot, executor, accrual)   [read-only bind]
        │
        ▼
   Fluent Bit  ──[tail → derive service label → redact]──► outbound TLS :443 ──► Grafana Cloud Loki
   (own compose service:                                                              │
    read_only, cap_drop ALL, no-new-privileges,                          Grafana Cloud hosted UI
    mem_limit ~64 MB, cpus cap, tmpfs buffer,                            (HTTPS + login + 2FA)
    NO docker socket, outbound-only)                                     14-day retention, search,
                                                                         dashboards, alerts
```

On the server: **only an outbound connection**. No inbound port is added (SSH:22 stays as
the sole inbound, unchanged).

## Components

### 1. Fluent Bit agent (new compose service `logs-agent`)

- **Image:** pinned `fluent/fluent-bit:<pinned-version>` (exact tag pinned; digest-pin in
  compose optional). No custom image to maintain.
- **Inputs:** `tail` over `/var/lib/docker/containers/*/*-json.log`, `Docker_Mode On`,
  `docker` JSON parser, a position DB on a small tmpfs/volume so restarts don't re-ship.
- **Service labels:** the JSON log *file path* contains the container **ID**, not name. A
  small **Lua filter** maps ID → service name by reading that container's
  `/var/lib/docker/containers/<id>/config.v2.json` (`.Name`), mounted read-only, and
  caches it. This yields a `service` label (bot/executor/accrual) with **no docker socket**.
  App services get explicit `container_name`s so the labels are stable across redeploys.
- **Labels emitted to Loki:** `service`, `container`, `level` (parsed from the structlog
  line when present). Low-cardinality only (Loki best practice).
- **Redaction filter (defense-in-depth):** before output, drop/rewrite anything sensitive.
  See "Redaction rules" below.
- **Output:** Fluent Bit `loki` plugin → Grafana Cloud endpoint, `tls On`, HTTP basic auth
  (user = instance/user id, password = access-policy token with **`logs:write` only**).
  Retry with bounded backoff; bounded memory buffer (`Mem_Buf_Limit`), drop-on-full rather
  than grow (never balloon the box).
- **Scope:** only `bot`, `executor`, `accrual`. **Postgres logs are not shipped** (noisy,
  low value). This is configurable but off by default.

### 2. Redaction rules (agent-side)

Secrets are already masked in-app (SecretStr, `****`), so this is belt-and-suspenders, but
required because logs leave the box:

- **Full EVM address** `0x[0-9a-fA-F]{40}` → `0x<redacted>`. Masked addresses
  (`0x2783…Ef72`, ellipsis) are NOT 40 contiguous hex, so they pass through unchanged.
- **Keyword-scoped secret redaction:** any line/field whose key or context matches
  `private_key | master_key | api_secret | passphrase | bot_token | secret | seed |
  authorization` → value replaced with `<redacted>`.
- **Do NOT blanket-redact `0x[0-9a-fA-F]{64}`** — that shape is a **txhash** (which we WANT
  in logs) and is indistinguishable from a raw private key by shape alone. Private keys are
  caught by the keyword rule above; they are never logged by the app in the first place.
- The redaction patterns live in one place and are covered by a unit test (below) as the
  source of truth for intent.

### 3. Credentials & config

- Grafana Cloud Loki push URL, user id, and `logs:write` token come from the host `.env`
  (chmod 600), injected as env vars into the agent, referenced by the Fluent Bit config.
  **Never** baked into an image or committed. `.env.example` gets placeholders +
  a short comment block.
- Fluent Bit config files (`fluent-bit.conf`, parsers, Lua) are committed under
  `ops/fluent-bit/` and mounted read-only into the container.

### 4. Security hardening (matches the existing app services, §9)

- `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`,
  `restart: unless-stopped`, `mem_limit` (~64–96 MB) and `cpus` cap so it can never starve
  the payout bot, `tmpfs` for the buffer/position DB.
- **No docker socket.** Read-only bind of `/var/lib/docker/containers` only.
- The agent runs as root **inside** the container only because the Docker log dir is
  root-owned; this is constrained by RO rootfs + RO mount + all caps dropped +
  no-new-privileges + no socket + outbound-only egress. (Alternative non-root paths were
  rejected: the docker-socket approach is root-equivalent and worse; the fluentd log-driver
  approach couples the payout bot's logging to agent availability.)
- **Resilience:** the agent reads files; it never sits in the app's logging path. If the
  agent is stopped, lagging, or OOM-killed, the bot/executor/accrual keep running and keep
  writing their own json-file logs — no coupling, no back-pressure on the payout path.

### 5. Grafana Cloud (owner-side setup, documented, not automated)

Manual one-time steps recorded in `ops/fluent-bit/README.md`:
1. Create a free Grafana Cloud account; **enable 2FA**.
2. Create an Access Policy + token scoped to **`logs:write`**; note the Loki push URL and
   user id.
3. Put URL/user/token into the server `.env`.
4. Import the provided dashboards; enable the provided alerts (route to email/Telegram).

### 6. Dashboards & alerts (log-derived, no metrics agent)

Provided as importable Grafana JSON + LogQL queries (owner imports them):

- **Overview:** log volume by `service`, `level` breakdown, live tail.
- **Payouts:** occurrences of `payout sent` / `payout paid` / `payout failed` /
  `pending_admin`, over time.
- **Accrual health:** last `run_cycle` markers; alert if silent > N hours.
- **Errors/alerts:** ERROR/WARN rate; dev-alert lines.
- **Alerts:** spike in ERROR, any `payout failed`, accrual silence → email/Telegram.
- **Status/"metrics" come from logs only** — no Prometheus/node-exporter (would cost RAM).
  Host RAM/CPU and live wallet balance are out of scope (wallet balance already lives in
  the admin bot).

## Testing

- **Unit (pytest):** the redaction patterns as a pure Python reference implementation —
  assert full addresses and keyword secrets are redacted, and that **txhashes and masked
  addresses survive**. This pins intent even though the agent runs the equivalent in Lua/
  filters.
- **Local integration (manual, documented):** run the pinned Fluent Bit image against a
  fixture json-log file; assert output records carry the right `service` label and contain
  no secret/full-address, then confirm a test push reaches Grafana Cloud.
- **Server smoke (documented):** after deploy, confirm the agent sees logs, pushes to
  Grafana Cloud, stays within `mem_limit`, and that **stopping the agent leaves the payout
  bot unaffected**.

## Out of scope (YAGNI)

- WireGuard / any VPN or inbound port (unnecessary once the UI is hosted off-box).
- Metrics agent / Prometheus / node-exporter (RAM cost on a 1 GB box).
- Exporting the DB `audit_events` table separately (docker logs already carry the business
  events the app logs via structlog).
- Shipping Postgres logs (off by default; can be enabled later).
- Freeing server RAM / removing the stale `commission-rebate-bot-bot-1` container or
  upgrading the VPS — separate operational decisions, not required by this design.

## Security summary

- No new inbound port; SSH:22 remains the only one. Agent is outbound-only TLS.
- No docker socket; read-only access to log files only.
- Agent hardened identically to app services; mem/cpu-capped so it can't starve the bot.
- Secrets never leave the box: masked in-app + agent-side redaction, with txhashes
  deliberately preserved.
- Grafana Cloud token scoped to `logs:write` only; account protected with 2FA.
- Payout path is fully decoupled from the agent's health.
