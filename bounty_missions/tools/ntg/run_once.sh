#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)

PYTHON_BIN=${PYTHON_BIN:-python3}
CONFIG_PATH=${NTG_SERVICE_CONFIG:-$SCRIPT_DIR/service.example.yaml}
LOG_DIR=${NTG_LOG_DIR:-$SCRIPT_DIR/out/logs}

mkdir -p "$LOG_DIR"
cd "$REPO_ROOT"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG_FILE="$LOG_DIR/service-$STAMP.log"

echo "[$STAMP] ntg service run start config=$CONFIG_PATH" >> "$LOG_FILE"
"$PYTHON_BIN" "$SCRIPT_DIR/service.py" --config "$CONFIG_PATH" >> "$LOG_FILE" 2>&1
echo "[$STAMP] ntg service run complete" >> "$LOG_FILE"
