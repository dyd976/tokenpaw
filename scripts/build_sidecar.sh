#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
TARGET_TRIPLE="${TARGET_TRIPLE:-$(rustc -vV | awk '/^host:/ {print $2}') }"
TARGET_TRIPLE="${TARGET_TRIPLE// /}"
OUTPUT_DIRECTORY="${OUTPUT_DIRECTORY:-desktop/src-tauri/binaries}"
FRONTEND_PATH="$ROOT_DIR/frontend"

"$PYTHON_BIN" -m PyInstaller --onefile --name agent-token-monitor-sidecar \
  --add-data "$FRONTEND_PATH:frontend" \
  --distpath "$OUTPUT_DIRECTORY" --workpath build/sidecar --specpath build/sidecar \
  agent_token_monitor/sidecar.py

BUILT_PATH="$OUTPUT_DIRECTORY/agent-token-monitor-sidecar"
TARGET_PATH="$OUTPUT_DIRECTORY/agent-token-monitor-sidecar-$TARGET_TRIPLE"
cp -f "$BUILT_PATH" "$TARGET_PATH"
chmod +x "$TARGET_PATH"
echo "Sidecar created at $TARGET_PATH."
