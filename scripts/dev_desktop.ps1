$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$desktopRoot = Join-Path $projectRoot "desktop"
$dataRoot = Join-Path $projectRoot "data"
$null = New-Item -ItemType Directory -Force -Path $dataRoot

$cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
if (Test-Path $cargoBin) {
  $env:Path = "$cargoBin;$env:Path"
}

# Keep the development API separate from a packaged app that may already be running.
$env:TOKEN_MONITOR_PORT = "8766"
$env:TOKEN_MONITOR_AUTOSCAN = "1"
$env:TOKEN_MONITOR_POLLING_INTERVAL = "5"
$env:TOKEN_MONITOR_STORE_PROMPT_CONTENT = "1"
$env:TOKEN_MONITOR_REPROCESS_PROMPTS = "1"
$env:TOKEN_MONITOR_DB = Join-Path $dataRoot "token-monitor-dev.db"
$env:VITE_API_BASE = "http://127.0.0.1:8766"

$apiLog = Join-Path $dataRoot "dev-api.log"
$apiErrorLog = Join-Path $dataRoot "dev-api.err.log"
$api = Start-Process -FilePath "python" -ArgumentList "-m", "agent_token_monitor.sidecar" -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $apiLog -RedirectStandardError $apiErrorLog

try {
  Push-Location $desktopRoot
  npm run tauri:dev
}
finally {
  Pop-Location
  if ($api -and -not $api.HasExited) {
    Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
  }
}
