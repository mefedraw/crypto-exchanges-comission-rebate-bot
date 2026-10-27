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
