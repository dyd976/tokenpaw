# Desktop companion

This directory contains the Tauri 2 desktop shell for the local token monitor.

## Development prerequisites

- Node.js 20+
- Rust toolchain and Cargo
- Windows: WebView2 and C++ build tools
- macOS: Xcode Command Line Tools

Install UI dependencies and run the browser shell:

```powershell
npm install
npm run dev
```

The browser shell expects the API at `http://127.0.0.1:8765`. Start the local API in another terminal with:

```powershell
$env:TOKEN_MONITOR_PORT = "8765"
agent-token-monitor dashboard --host 127.0.0.1 --port 8765
```

When Rust is installed, run `npm run tauri:dev` to test the transparent pet window. The packaged app should include the PyInstaller sidecar built with `scripts/build_sidecar.ps1` on Windows or `bash scripts/build_sidecar.sh` on macOS; Tauri target-specific sidecar names are required for release builds.

For the fastest development loop on Windows, run `npm run dev:desktop` from this directory. It starts a development-only API on port `8766` with a separate `data/token-monitor-dev.db`, launches the Tauri pet, and stops the API when Tauri exits. React/CSS changes are reflected by Vite/Tauri hot reload; no installer rebuild is needed. On macOS, run `bash ../scripts/dev_desktop.sh` from this directory. Use `npm run tauri:dev` directly only when an API is already running on port `8765`.

On macOS, run the build from a macOS host so PyInstaller produces a native sidecar, then package the DMG:

```bash
npm run build
bash ../scripts/build_sidecar.sh
npm run tauri:build -- --bundles dmg
```

The same release flow is available from the repository root as a single command:

```bash
bash scripts/build_desktop_macos.sh
```

The script intentionally builds on the current macOS host; it does not cross-compile the Python sidecar. On Apple silicon it produces an `aarch64-apple-darwin` sidecar and DMG, while an Intel Mac produces an `x86_64-apple-darwin` sidecar and DMG.

The shell script uses the host Rust target (for example `aarch64-apple-darwin` on Apple silicon). Override it with `TARGET_TRIPLE` when building for a specific installed Rust target.

The current pet is code-native SVG/CSS-style markup, so no external image or network asset is needed. Search uses the local `/api/search` endpoint and only returns metadata or content explicitly stored by the privacy settings.

Packaged builds enable a 5-second incremental scan of `~/.claude/projects` and `~/.codex/sessions`. The pet opens a left-upper diagnostics popover and drills down through Agent → Project → Session → Prompt / Turn. The hierarchy period selector (`Today`, `7 days`, `All time`) is applied consistently to each level, including the Prompt / Turn list. For Codex, the human-readable `thread_name` from the local `~/.codex/session_index.jsonl` is shown as the session name when available; raw session IDs remain available as fallback. Optional local pricing is read from `TOKEN_MONITOR_PRICING_PATH`; unmatched prices remain Unknown.
