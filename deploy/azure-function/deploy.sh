#!/usr/bin/env bash
# Deploy the page-telemetry collector as an Azure Function.
#
# Usage:
#   ./deploy.sh <resource-group> <function-app-name>
#
# See deploy.ps1 for prereqs.
set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <resource-group> <function-app-name> [collector-path]" >&2
    exit 1
fi

RG="$1"
APP="$2"
COLLECTOR_PATH="${3:-../../etl/collector.py}"

cd "$(dirname "$0")"

echo "1/3  Copying collector.py from $COLLECTOR_PATH …"
cp -f "$COLLECTOR_PATH" ./collector.py
trap 'rm -f ./collector.py' EXIT

echo "2/3  Publishing to $APP …"
func azure functionapp publish "$APP" --python

echo "3/3  Done."
echo ""
echo "Verify with:"
echo "  az functionapp function show -g $RG -n $APP --function-name daily_page_telemetry"
echo "  az functionapp logs tail -g $RG -n $APP"
