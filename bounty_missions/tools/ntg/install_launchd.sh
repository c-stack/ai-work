#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)

TARGET_PLIST=${1:-$HOME/Library/LaunchAgents/com.ntg.radar.plist}
TEMPLATE_PATH="$SCRIPT_DIR/launchd/com.ntg.radar.plist.template"
CONFIG_PATH=${NTG_SERVICE_CONFIG:-$SCRIPT_DIR/service.example.yaml}
LOG_DIR=${NTG_LOG_DIR:-$SCRIPT_DIR/out/logs}
RUN_SCRIPT="$SCRIPT_DIR/run_once.sh"

mkdir -p "$(dirname "$TARGET_PLIST")" "$LOG_DIR"

escape_sed() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

WORKDIR_ESCAPED=$(escape_sed "$REPO_ROOT")
SCRIPT_ESCAPED=$(escape_sed "$RUN_SCRIPT")
CONFIG_ESCAPED=$(escape_sed "$CONFIG_PATH")
LOGDIR_ESCAPED=$(escape_sed "$LOG_DIR")

sed \
  -e "s|__WORKDIR__|$WORKDIR_ESCAPED|g" \
  -e "s|__SCRIPT__|$SCRIPT_ESCAPED|g" \
  -e "s|__CONFIG__|$CONFIG_ESCAPED|g" \
  -e "s|__LOG_DIR__|$LOGDIR_ESCAPED|g" \
  "$TEMPLATE_PATH" > "$TARGET_PLIST"

launchctl unload "$TARGET_PLIST" >/dev/null 2>&1 || true
launchctl load "$TARGET_PLIST"

echo "installed launch agent: $TARGET_PLIST"
echo "logs directory: $LOG_DIR"
