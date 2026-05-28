#!/usr/bin/env bash
# run-collector.sh
# Wrapper for scheduling the collector via cron or systemd timer.
#
# Usage (cron):
#   0 6 * * *  /opt/powerbi-page-telemetry/deploy/local/run-collector.sh
#
# Reads secrets from environment variables. For systemd, set them in an
# EnvironmentFile= drop-in; for cron, source a file with the env vars
# inside a wrapper or use the systemd path.
set -euo pipefail

REPO_ETL="${REPO_ETL:-$(cd "$(dirname "$0")/../../etl" && pwd)}"
OUTPUT_DIR="${PBI_OUTPUT_DIR:-$HOME/PowerBITelemetry}"
LOG_DIR="${LOG_DIR:-$HOME/PowerBITelemetry/logs}"

mkdir -p "$OUTPUT_DIR" "$LOG_DIR"
LOG="$LOG_DIR/collector-$(date -u +%Y%m%d-%H%M%S).log"

export PYTHONIOENCODING=utf-8
export PBI_OUTPUT_DIR="$OUTPUT_DIR"

for v in PBI_TENANT_ID PBI_CLIENT_ID PBI_CLIENT_SECRET; do
    if [[ -z "${!v:-}" ]]; then
        echo "ERROR: required env var $v is unset" >&2
        exit 2
    fi
done

echo "Running: python $REPO_ETL/collector.py  (output -> $OUTPUT_DIR; log -> $LOG)"
python "$REPO_ETL/collector.py" 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
