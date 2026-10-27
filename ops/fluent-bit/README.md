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
docker compose up -d --no-deps logs-agent
docker compose logs -f logs-agent   # should show it tailing + pushing, no errors
```
**Warning:** this branch adds `container_name`s to the bot/executor/accrual services. The
next plain `docker compose up -d` (without `--no-deps`) will recreate — i.e. restart — the
whole payout stack once, since Compose considers those services changed. Use `--no-deps`
here to bring up only `logs-agent`, and do the full-stack recreate deliberately/at a safe time.

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
  values → `<redacted>`. Covers both the logfmt `key=value`/`key: value` form and the
  production JSON `"key":"value"` form (structlog's `JSONRenderer` output).
- Only the 3 rebate services are shipped; Postgres and all other host containers are dropped
  by a keep-filter. A `grep` filter (`ops/fluent-bit/fluent-bit.conf`) runs after the
  `service-label.lua` filter and keeps only records whose `service` label matches
  `^rebate-(bot|executor|accrual)$`, before redaction and egress — this drops Postgres
  (`rebate-bot-postgres-1`), the log agent's own logs, and every unrelated container on the
  host (e.g. other projects' containers).
- `redact.lua` must stay in sync with `tests/test_log_redaction.py` — change both together.

## Useful LogQL
Logs ship as JSON (structlog `JSONRenderer`), e.g. `{"event":"...","level":"info",...}`.
- Errors/warnings: `{job="rebate-bot"} | json | level=~"error|warning"`
- Payout events: `{job="rebate-bot"} |~ "payout (broadcast sent|confirmed & paid|failed)"`
- Accrual cycles: `{job="rebate-bot", service="rebate-accrual"} |~ "run_cycle|accrual"`
- Per-service volume: `sum by (service) (count_over_time({job="rebate-bot"}[5m]))`

## Suggested alerts (Grafana Cloud Alerting)
- Any `payout ... failed` in 5m → notify.
- ERROR rate spike: `sum(count_over_time({job="rebate-bot"} | json | level="error" [10m])) > 0`
  (equivalently, the label-free form: `{job="rebate-bot"} |~ "\"level\":\"error\""`).
- Accrual silence: no `rebate-accrual` lines in 24h.
