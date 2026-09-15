#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/desktop"

command -v npm >/dev/null 2>&1 || { echo "npm is required" >&2; exit 1; }
command -v cargo >/dev/null 2>&1 || { echo "Cargo is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

npm run build
bash "$ROOT_DIR/scripts/build_sidecar.sh"
npm run tauri:build -- --bundles dmg

echo "macOS DMG build completed."
