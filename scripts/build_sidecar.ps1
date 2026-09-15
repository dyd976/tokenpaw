param(
  [string]$OutputDirectory = "desktop/src-tauri/binaries",
  [string]$TargetTriple = "x86_64-pc-windows-msvc"
)

$ErrorActionPreference = "Stop"
$frontendPath = (Resolve-Path "frontend").Path
python -m PyInstaller --onefile --name agent-token-monitor-sidecar --add-data "$frontendPath;frontend" --distpath $OutputDirectory --workpath build/sidecar --specpath build/sidecar agent_token_monitor/sidecar.py
$builtPath = Join-Path $OutputDirectory "agent-token-monitor-sidecar.exe"
$targetPath = Join-Path $OutputDirectory "agent-token-monitor-sidecar-$TargetTriple.exe"
Copy-Item -LiteralPath $builtPath -Destination $targetPath -Force
Write-Host "Sidecar created at $targetPath."
