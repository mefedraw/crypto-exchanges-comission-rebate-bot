# Log Shipping (Fluent Bit → Grafana Cloud) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the bot/executor/accrual Docker logs off-box to Grafana Cloud (free tier, 14-day retention) via a hardened, outbound-only Fluent Bit agent, with agent-side redaction and zero new inbound ports.

**Architecture:** A new `logs-agent` Docker Compose service runs Fluent Bit, tails the Docker JSON log files read-only, derives a `service` label from each container's `config.v2.json` (no docker socket), extracts a `level`, redacts secrets/full-addresses (preserving txhashes), and pushes over outbound TLS to the Grafana Cloud Loki endpoint. A Python reference implementation of the redaction rules is unit-tested as the source of truth for intent; the agent's Lua mirrors it.

**Tech Stack:** Fluent Bit 4.2.0 (pinned image), Lua filters, Docker Compose, Grafana Cloud Loki, Python 3.11 + pytest (redaction reference test only).

## Global Constraints

- **No new inbound port.** The agent is outbound-only TLS to Grafana Cloud. SSH:22 stays the sole inbound.
- **No docker socket.** The agent reads `/var/lib/docker/containers` **read-only** only.
- **Agent hardened like app services (compose §9):** `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `restart: unless-stopped`, plus `mem_limit` ≤ 96m and a `cpus` cap so it can never starve the payout bot.
- **Payout path decoupled:** the agent reads files; it is never in the app's logging path. Stopping/lagging/OOM-killing the agent must not affect bot/executor/accrual.
- **Redaction is mandatory before egress:** full `0x`+40-hex EVM addresses → `0x<redacted>`; secret-keyword values → `<redacted>`; **txhash (`0x`+64-hex) and masked addresses (`0x2783…Ef72`) are preserved.**
- **Secrets only from `.env` (chmod 600), never in the image or git.** Grafana Cloud token scoped to `logs:write` only.
- **New pytest test files must be FLAT** (`tests/test_*.py`), never a new `tests/<pkg>/` directory (a new package dir breaks ~37 tests via a testcontainers session-loop ordering bug).
- **Ship only** `bot`, `executor`, `accrual`. **Postgres logs are NOT shipped** (off by default).
- Money/secret rules from CLAUDE.md remain in force.

---

### Task 1: Redaction reference + unit test (source of truth for intent)

The agent runs redaction in Lua (Task 2). This task creates the authoritative Python
reference + tests that pin exactly what "redacted" means, with concrete examples the Lua
must reproduce. Kept in the flat test file itself (its only purpose is to specify intent).

**Files:**
- Create/Test: `tests/test_log_redaction.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `redact_line(text: str) -> str` (module-level function in the test file). The
  Task 2 Lua mirrors these exact behaviors. Canonical examples:
  - `0x` + 40 hex → `0x<redacted>`
  - `0x` + 64 hex (txhash) → unchanged
  - `0x2783…Ef72` (masked, ellipsis) → unchanged
  - `bot_token=123:abc` → `bot_token=<redacted>`
  - `api_secret: deadbeef` → `api_secret: <redacted>`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_log_redaction.py
"""Reference (source-of-truth) redaction for logs shipped off-box.

The Fluent Bit agent (ops/fluent-bit/redact.lua) redacts in Lua before pushing to
Grafana Cloud; this module is the authoritative Python spec of WHAT must be redacted,
covered by tests. The Lua mirrors these behaviors (kept in sync by hand; see
ops/fluent-bit/README.md). Secrets are already masked in-app — this is defense-in-depth
because logs leave the server.
"""

from __future__ import annotations

import re

# A run of `0x` followed by hex. The callback decides by LENGTH: exactly 40 hex is an
# EVM address (redact); 64 hex is a txhash (keep — we want it in logs); a masked address
# uses an ellipsis so it is not a contiguous hex run and never matches.
_HEX0X = re.compile(r"0x[0-9a-fA-F]+")
# key=value / key: value where the key is a secret. Case-insensitive.
_SECRET_KV = re.compile(
    r"(?i)\b(private_key|master_key|api_secret|passphrase|bot_token|secret|seed|authorization)"
    r"(\s*[=:]\s*)(\S+)"
)


def redact_line(text: str) -> str:
    def _addr(m: re.Match[str]) -> str:
        tok = m.group(0)
        return "0x<redacted>" if len(tok) - 2 == 40 else tok

    text = _HEX0X.sub(_addr, text)
    text = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    return text


def test_full_evm_address_is_redacted():
    addr = "0x" + "a1b2c3d4" * 5  # exactly 40 hex chars
    assert redact_line(f"paying {addr} now") == "paying 0x<redacted> now"


def test_txhash_is_preserved():
    txh = "0x" + "ab" * 32  # 64 hex chars
    assert redact_line(f"broadcast txhash={txh}") == f"broadcast txhash={txh}"


def test_masked_address_is_preserved():
    line = "wallet 0x2783…Ef72 balance ok"
    assert redact_line(line) == line


def test_bot_token_value_is_redacted():
    assert redact_line("start bot_token=123456:AAbbCC done") == "start bot_token=<redacted> done"


def test_api_secret_colon_form_is_redacted():
    assert redact_line("cfg api_secret: deadbeefcafe") == "cfg api_secret: <redacted>"


def test_plain_message_is_unchanged():
    line = "payout confirmed & paid pid=42 confirmations=15"
    assert redact_line(line) == line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_log_redaction.py -v`
Expected: collection succeeds but FAILS — actually with the reference included it should
PASS immediately. To honor TDD, first paste ONLY the test functions (not `redact_line` /
regexes), run, and see `NameError: name 'redact_line' is not defined` → FAIL. Then add the
implementation (regexes + `redact_line`).

- [ ] **Step 3: Add the implementation**

Add the module docstring, imports, `_HEX0X`, `_SECRET_KV`, and `redact_line` shown above.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_log_redaction.py -v`
Expected: 6 passed.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check tests/test_log_redaction.py && uv run ruff format tests/test_log_redaction.py
git add tests/test_log_redaction.py
git commit -m "test(logs): redaction reference + tests (addresses redacted, txhash kept)"
```

---

### Task 2: Fluent Bit config — tail, service label, redact, Loki output

**Files:**
- Create: `ops/fluent-bit/fluent-bit.conf`
- Create: `ops/fluent-bit/service-label.lua`
- Create: `ops/fluent-bit/redact.lua`
- Create: `ops/fluent-bit/selftest.sh`

**Interfaces:**
- Consumes: redaction intent from Task 1 (`tests/test_log_redaction.py`).
- Produces: a Fluent Bit config that emits records with fields `log`, `service`, `level`
  and pushes to Loki with labels `job=rebate-bot, service=<svc>, level=<lvl>`. Env vars it
  reads: `GRAFANA_LOKI_HOST`, `GRAFANA_LOKI_USER`, `GRAFANA_LOKI_TOKEN` (Task 3 supplies
  them via `.env`).

- [ ] **Step 1: Write `ops/fluent-bit/service-label.lua`**

```lua
-- Map a Docker container id (from the tail file path) to its service name by reading
-- the container's config.v2.json (mounted read-only). No docker socket. Cached per id.
local cache = {}

function set_service(tag, ts, record)
    local file = record["file"]
    if file == nil then
        record["service"] = "unknown"
        return 2, ts, record
    end
    local id = string.match(file, "containers/([0-9a-f]+)/")
    if id == nil then
        record["service"] = "unknown"
        return 2, ts, record
    end
    local name = cache[id]
    if name == nil then
        local fh = io.open("/var/lib/docker/containers/" .. id .. "/config.v2.json", "r")
        if fh ~= nil then
            local data = fh:read("*a")
            fh:close()
            -- config.v2.json stores "Name":"/rebate-bot-bot-1"
            name = string.match(data, '"Name":"/?([^"]+)"')
            if name ~= nil then cache[id] = name end
        end
    end
    record["service"] = name or "unknown"
    return 2, ts, record
end
```

- [ ] **Step 2: Write `ops/fluent-bit/redact.lua`**

Mirrors `tests/test_log_redaction.py` (Task 1). Also extracts a `level` from the structlog
console line (`... [info     ] ...`).

```lua
-- Redact secrets/full-addresses before egress; extract a coarse level. Mirrors the
-- Python reference in tests/test_log_redaction.py (kept in sync by hand).
local SECRET_KEYS = {
    "private_key", "master_key", "api_secret", "passphrase",
    "bot_token", "secret", "seed", "authorization",
}

local function redact_str(s)
    if type(s) ~= "string" then return s end
    -- 0x + hex run: redact ONLY when exactly 40 hex (EVM address). 64-hex txhash kept.
    s = s:gsub("0x%x+", function(tok)
        if #tok - 2 == 40 then return "0x<redacted>" end
        return tok
    end)
    -- key=value / key: value where key is a secret (case-insensitive via lower copy).
    local lower = s:lower()
    for _, kw in ipairs(SECRET_KEYS) do
        local from = 1
        while true do
            local a, b = lower:find(kw .. "%s*[=:]%s*%S+", from)
            if a == nil then break end
            local prefix = s:sub(a, s:find("[=:]", a))
            s = s:sub(1, a - 1) .. prefix .. " <redacted>" .. s:sub(b + 1)
            lower = s:lower()
            from = a + #prefix + 12
        end
    end
    return s
end

local function level_of(s)
    if type(s) ~= "string" then return nil end
    local lvl = string.match(s, "%[%s*(%a+)%s*%]")
    if lvl ~= nil then return lvl:lower() end
    return nil
end

function redact(tag, ts, record)
    local lvl = nil
    for k, v in pairs(record) do
        if lvl == nil then lvl = level_of(v) end
        record[k] = redact_str(v)
    end
    record["level"] = lvl or "unknown"
    return 2, ts, record
end
```

- [ ] **Step 3: Write `ops/fluent-bit/fluent-bit.conf`**

```ini
[SERVICE]
    Flush            5
    Daemon           off
    Log_Level        info
    Parsers_File     /fluent-bit/etc/parsers.conf

[INPUT]
    Name             tail
    Path             /var/lib/docker/containers/*/*.log
    Path_Key         file
    Multiline.Parser docker
    Tag              docker.*
    DB               /buffer/tail.db
    Mem_Buf_Limit    16MB
    Skip_Long_Lines  On
    Refresh_Interval 5

[FILTER]
    Name    lua
    Match   docker.*
    Script  /cfg/service-label.lua
    Call    set_service

[FILTER]
    Name    lua
    Match   docker.*
    Script  /cfg/redact.lua
    Call    redact

[OUTPUT]
    Name            loki
    Match           docker.*
    Host            ${GRAFANA_LOKI_HOST}
    Port            443
    Tls             On
    Tls.verify      On
    Http_User       ${GRAFANA_LOKI_USER}
    Http_Passwd     ${GRAFANA_LOKI_TOKEN}
    Labels          job=rebate-bot, $service, $level
    Remove_keys     file,stream,time,service,level
    Line_format     json
    Drop_single_key On
```

Note: after `Remove_keys` strips everything but `log`, `Drop_single_key On` sends the raw
message string as the Loki line, with `service`/`level` promoted to labels.

- [ ] **Step 4: Write `ops/fluent-bit/selftest.sh`**

```bash
#!/usr/bin/env bash
# Validate the Fluent Bit config parses under the pinned image (no network).
set -euo pipefail
IMG="fluent/fluent-bit:4.2.0"
docker run --rm \
  -e GRAFANA_LOKI_HOST=example.invalid \
  -e GRAFANA_LOKI_USER=0 \
  -e GRAFANA_LOKI_TOKEN=x \
  -v "$(pwd)/ops/fluent-bit:/cfg:ro" \
  "$IMG" /fluent-bit/bin/fluent-bit -c /cfg/fluent-bit.conf --dry-run
echo "fluent-bit config OK"
```

- [ ] **Step 5: Run the config self-test**

Run: `bash ops/fluent-bit/selftest.sh`
Expected: ends with `configuration test is successful` (Fluent Bit dry-run) and
`fluent-bit config OK`. If the loki/lua plugins report a parse error, fix the config.

- [ ] **Step 6: Commit**

```bash
chmod +x ops/fluent-bit/selftest.sh
git add ops/fluent-bit/fluent-bit.conf ops/fluent-bit/service-label.lua \
        ops/fluent-bit/redact.lua ops/fluent-bit/selftest.sh
git commit -m "feat(logs): Fluent Bit config — tail, service label, redact, Loki output"
```

---

### Task 3: Compose `logs-agent` service, stable container names, `.env.example`

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `ops/fluent-bit/*` (Task 2).
- Produces: a hardened `logs-agent` service; stable `container_name`s on the app services
  so the derived `service` label is clean and stable across redeploys.

- [ ] **Step 1: Add stable container names to the app services**

In `docker-compose.yml`, add one `container_name` line to each app service (these cannot
live in the `&app` anchor — they must be unique per service):

```yaml
  bot:
    <<: *app
    container_name: rebate-bot
    command: ["python", "-m", "bot.main"]

  accrual:
    <<: *app
    container_name: rebate-accrual
    command: ["python", "-m", "bot.accrual"]

  executor:
    <<: *app
    container_name: rebate-executor
    command: ["python", "-m", "bot.executor"]
```

- [ ] **Step 2: Add the `logs-agent` service and its buffer volume**

Add under `services:` (agent runs as root ONLY because the Docker log dir is root-owned;
constrained by RO rootfs + RO mounts + all caps dropped + no-new-privileges + no socket +
mem/cpu caps + outbound-only):

```yaml
  logs-agent:
    image: fluent/fluent-bit:4.2.0
    restart: unless-stopped
    command: ["/fluent-bit/bin/fluent-bit", "-c", "/cfg/fluent-bit.conf"]
    env_file:
      - .env
    volumes:
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - ./ops/fluent-bit:/cfg:ro
      - logs_agent_buffer:/buffer
    read_only: true
    user: root
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: 96m
    cpus: "0.5"
    depends_on:
      - bot
      - executor
      - accrual
    logging:
      driver: json-file
      options:
        max-size: "5m"
        max-file: "2"
```

And add the volume at the bottom next to `pgdata`:

```yaml
volumes:
  pgdata:
  logs_agent_buffer:
```

- [ ] **Step 3: Add Grafana Cloud placeholders to `.env.example`**

Append:

```bash
# ---- Log shipping (Fluent Bit -> Grafana Cloud Loki) ----
# Grafana Cloud -> Connections -> Loki: copy the host (WITHOUT https:// and WITHOUT the
# /loki/... path) and the numeric user/instance id. Create an Access Policy token scoped
# to `logs:write` only. Enable 2FA on the Grafana Cloud account. Never commit real values.
GRAFANA_LOKI_HOST=logs-prod-000.grafana.net
GRAFANA_LOKI_USER=000000
GRAFANA_LOKI_TOKEN=glc_replace_me
```

- [ ] **Step 4: Validate compose renders**

Run: `docker compose config -q`
Expected: exit 0, no error. (Add dummy values to a local `.env` first if it complains
about unset variables: `GRAFANA_LOKI_HOST=x`, `GRAFANA_LOKI_USER=0`, `GRAFANA_LOKI_TOKEN=x`.)

Then confirm the agent is hardened:
Run: `docker compose config | grep -A3 -E "read_only|cap_drop|mem_limit"`
Expected: shows `read_only: true`, `cap_drop: [ALL]`, `mem_limit` on `logs-agent`.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "feat(logs): hardened logs-agent compose service + stable names + env example"
```

---

### Task 4: Runbook + Grafana dashboard

**Files:**
- Create: `ops/fluent-bit/README.md`
- Create: `ops/grafana/dashboards/rebate-logs.json`

**Interfaces:**
- Consumes: everything above.
- Produces: owner-facing setup/runbook + an importable dashboard.

- [ ] **Step 1: Write `ops/fluent-bit/README.md`**

````markdown
# Log shipping: Fluent Bit → Grafana Cloud

Ships `rebate-bot` / `rebate-executor` / `rebate-accrual` Docker logs off-box to Grafana
Cloud Loki over outbound TLS. No inbound port is opened. Secrets are redacted before egress
(see `redact.lua`, mirrored by `tests/test_log_redaction.py`).

## One-time owner setup
1. Create a free Grafana Cloud account. **Enable 2FA.**
2. Connections → Loki: copy the **host** (e.g. `logs-prod-012.grafana.net`, no `https://`,
   no path) and the numeric **user/instance id**.
3. Create an **Access Policy** + token scoped to **`logs:write` only**. Copy the token.
4. On the server, put the three values in `.env` (chmod 600):
   `GRAFANA_LOKI_HOST`, `GRAFANA_LOKI_USER`, `GRAFANA_LOKI_TOKEN`.
5. Import `ops/grafana/dashboards/rebate-logs.json` (Dashboards → Import).

## Deploy
```bash
docker compose up -d logs-agent
docker compose logs -f logs-agent   # should show it tailing + pushing, no errors
```

## Verify (server smoke)
- In Grafana Cloud → Explore (Loki datasource): `{job="rebate-bot"}` shows live logs.
- Labels present: `service` (rebate-bot/executor/accrual), `level`.
- Memory within cap: `docker stats --no-stream rebate-... logs-agent` (agent ≤ ~96 MB).
- **Decoupling check:** `docker compose stop logs-agent` → confirm bot/executor/accrual
  keep running and still write local logs (`docker compose logs -f bot`). Then
  `docker compose start logs-agent`.

## Redaction (what leaves the box)
- Full `0x`+40-hex addresses → `0x<redacted>`. Masked `0x…` and 64-hex **txhashes kept**.
- `private_key|master_key|api_secret|passphrase|bot_token|secret|seed|authorization`
  values → `<redacted>`.
- Postgres logs are NOT shipped.
- `redact.lua` must stay in sync with `tests/test_log_redaction.py` — change both together.

## Useful LogQL
- Errors/warnings: `{job="rebate-bot"} |~ "(?i)\\[(error|warning)"`
- Payout events: `{job="rebate-bot"} |~ "payout (broadcast sent|confirmed & paid|failed)"`
- Accrual cycles: `{job="rebate-bot", service="rebate-accrual"} |~ "run_cycle|accrual"`
- Per-service volume: `sum by (service) (count_over_time({job="rebate-bot"}[5m]))`

## Suggested alerts (Grafana Cloud Alerting)
- Any `payout ... failed` in 5m → notify.
- ERROR rate spike: `sum(count_over_time({job="rebate-bot"} |~ "(?i)\\[error" [10m])) > 0`.
- Accrual silence: no `rebate-accrual` lines in 24h.
````

- [ ] **Step 2: Write `ops/grafana/dashboards/rebate-logs.json`**

```json
{
  "title": "Rebate Bot — Logs",
  "schemaVersion": 39,
  "editable": true,
  "time": { "from": "now-24h", "to": "now" },
  "templating": {
    "list": [
      {
        "name": "svc",
        "type": "custom",
        "query": "rebate-bot,rebate-executor,rebate-accrual",
        "current": { "text": "All", "value": "$__all" },
        "includeAll": true,
        "multi": true
      }
    ]
  },
  "panels": [
    {
      "id": 1,
      "title": "Log volume by service",
      "type": "timeseries",
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 0 },
      "datasource": { "type": "loki" },
      "targets": [
        { "refId": "A", "expr": "sum by (service) (count_over_time({job=\"rebate-bot\"}[5m]))" }
      ]
    },
    {
      "id": 2,
      "title": "Payout events",
      "type": "logs",
      "gridPos": { "h": 10, "w": 24, "x": 0, "y": 8 },
      "datasource": { "type": "loki" },
      "targets": [
        { "refId": "A", "expr": "{job=\"rebate-bot\"} |~ \"payout (broadcast sent|confirmed & paid|failed)\"" }
      ]
    },
    {
      "id": 3,
      "title": "Errors & warnings",
      "type": "logs",
      "gridPos": { "h": 10, "w": 24, "x": 0, "y": 18 },
      "datasource": { "type": "loki" },
      "targets": [
        { "refId": "A", "expr": "{job=\"rebate-bot\"} |~ \"(?i)\\\\[(error|warning)\"" }
      ]
    }
  ]
}
```

- [ ] **Step 3: Validate the dashboard JSON**

Run: `python -c "import json; json.load(open('ops/grafana/dashboards/rebate-logs.json')); print('dashboard JSON OK')"`
Expected: `dashboard JSON OK`.

- [ ] **Step 4: Commit**

```bash
git add ops/fluent-bit/README.md ops/grafana/dashboards/rebate-logs.json
git commit -m "docs(logs): Grafana Cloud runbook + importable logs dashboard"
```

---

## Deployment (after all tasks; owner-gated, not part of task commits)

1. Owner adds the three `GRAFANA_LOKI_*` values to the server `.env` (chmod 600).
2. Sync + start only the agent:
   ```bash
   git archive HEAD | ssh -i ~/.ssh/rebate_deploy root@192.0.2.10 "tar -xf - -C /root/rebate-bot"
   ssh -i ~/.ssh/rebate_deploy root@192.0.2.10 "cd /root/rebate-bot && docker compose up -d logs-agent"
   ```
   (The `container_name` additions mean `bot`/`executor`/`accrual` are recreated once on the
   next full `up -d` — schedule that deliberately, not silently, since it restarts the
   payout stack.)
3. Run the server smoke checks in the runbook, including the decoupling check.

## Self-review notes (coverage vs spec)

- Spec §1 agent / §"Why Fluent Bit" → Task 2 + Task 3 (pinned 4.2.0, tail, no socket).
- Spec §2 redaction (address-length-aware, keyword, txhash preserved) → Task 1 (tested) +
  Task 2 (`redact.lua`).
- Spec §3 credentials from `.env`, `.env.example` placeholders → Task 3.
- Spec §4 hardening (read_only, cap_drop, no-new-privileges, mem/cpu caps, no socket,
  resilience) → Task 3 + runbook decoupling check.
- Spec §5 Grafana Cloud setup steps → Task 4 runbook.
- Spec §6 dashboards/alerts (log-derived, no metrics agent) → Task 4.
- Spec "Testing" (unit redaction, local integration dry-run, server smoke) → Task 1
  (unit), Task 2 (`selftest.sh` dry-run), Task 4 runbook (server smoke).
- Out-of-scope items (WireGuard, metrics agent, DB audit export, postgres logs) → excluded.
