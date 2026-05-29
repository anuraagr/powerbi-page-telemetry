#!/usr/bin/env bash
# run-collector.sh
# Wrapper for scheduling the collector via cron or systemd timer.
#
# Usage (cron):
#   0 6 * * *  /opt/powerbi-page-telemetry/deploy/local/run-collector.sh
#
# Mock-mode evaluation (no credentials required):
#   ./run-collector.sh --mock
#
# Reads secrets from environment variables. For systemd, set them in an
# EnvironmentFile= drop-in; for cron, source a file with the env vars
# inside a wrapper or use the systemd path.
set -euo pipefail

# Parse flags. --mock skips the env-var guard and runs collector with --mock.
MOCK=0
for arg in "$@"; do
    case "$arg" in
        --mock) MOCK=1 ;;
        -h|--help)
            sed -n '1,16p' "$0"
            exit 0 ;;
    esac
done

REPO_ETL="${REPO_ETL:-$(cd "$(dirname "$0")/../../etl" && pwd)}"
OUTPUT_DIR="${PBI_OUTPUT_DIR:-$HOME/PowerBITelemetry}"
LOG_DIR="${LOG_DIR:-$HOME/PowerBITelemetry/logs}"

mkdir -p "$OUTPUT_DIR" "$LOG_DIR"
LOG="$LOG_DIR/collector-$(date -u +%Y%m%d-%H%M%S).log"

export PYTHONIOENCODING=utf-8
export PBI_OUTPUT_DIR="$OUTPUT_DIR"

if [[ "$MOCK" -eq 0 ]]; then
    for v in PBI_TENANT_ID PBI_CLIENT_ID PBI_CLIENT_SECRET; do
        if [[ -z "${!v:-}" ]]; then
            echo "ERROR: required env var $v is unset (pass --mock to evaluate without credentials)" >&2
            exit 2
        fi
    done
    ARGS=()
else
    echo "[mock] running collector against bundled sample data — no Power BI credentials required"
    ARGS=(--mock)
fi

echo "Running: python $REPO_ETL/collector.py ${ARGS[*]:-}  (output -> $OUTPUT_DIR; log -> $LOG)"
# Prefer python3 (Ubuntu / most Linux distros) and fall back to python (RHEL,
# macOS Homebrew, Windows). The collector is Python 3.10+ on both names.
PY="$(command -v python3 || command -v python || true)"
if [[ -z "$PY" ]]; then
    echo "ERROR: neither 'python3' nor 'python' is on PATH. Install Python 3.10+ first." >&2
    exit 127
fi
"$PY" "$REPO_ETL/collector.py" "${ARGS[@]}" 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
