# Keep Beam Farm Ops reachable on a free public URL.
# Leave this PowerShell window open. Re-run if you see Cloudflare error 1033.

$ErrorActionPreference = "Stop"
$port = 8002
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$cf = Join-Path $env:LOCALAPPDATA "cloudflared\cloudflared.exe"

function Test-App {
  try {
    Invoke-WebRequest -Uri "http://127.0.0.1:$port/login" -UseBasicParsing -TimeoutSec 3 | Out-Null
    return $true
  } catch { return $false }
}

if (-not (Test-App)) {
  Write-Host "Starting Beam Farm Ops on port $port..."
  if (-not (Test-Path $python)) { throw "Missing $python — create the venv first." }
  Start-Process -FilePath $python -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","$port" -WorkingDirectory $root -WindowStyle Minimized
  for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep 1
    if (Test-App) { break }
  }
  if (-not (Test-App)) { throw "App did not start on port $port." }
}

if (-not (Test-Path $cf)) {
  Write-Host "Downloading cloudflared..."
  New-Item -ItemType Directory -Force -Path (Split-Path $cf) | Out-Null
  Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $cf -UseBasicParsing
}

Get-Process -Name cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host ""
Write-Host "Tunnel starting. Copy the https://....trycloudflare.com URL from the lines below."
Write-Host "Login: admin / farm2026"
Write-Host "Keep this window open. Error 1033 means this window closed or the PC slept."
Write-Host ""
& $cf tunnel --url "http://127.0.0.1:$port" --protocol http2
