#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_ROOT="$PROJECT_ROOT/data"
mkdir -p "$DATA_ROOT"

# Keep the development API separate from a packaged app that may already be running.
export TOKEN_MONITOR_PORT="8766"
export TOKEN_MONITOR_AUTOSCAN="1"
export TOKEN_MONITOR_POLLING_INTERVAL="5"
export TOKEN_MONITOR_DB="$DATA_ROOT/token-monitor-dev.db"
export VITE_API_BASE="http://127.0.0.1:8766"

python -m agent_token_monitor.sidecar > "$DATA_ROOT/dev-api.log" 2> "$DATA_ROOT/dev-api.err.log" &
API_PID=$!
trap 'kill "$API_PID" 2>/dev/null || true' EXIT

cd "$PROJECT_ROOT/desktop"
npm run tauri:dev
