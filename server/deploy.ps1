# ============================================================
# PolitePuppy one-click server deployment (Windows Server + BT panel)
#
# Usage (PowerShell, run as Administrator):
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 ^
#     -Port 8765 -AllowedHosts "dog.example.com" ^
#     -BaseUrl "https://api.deepseek.com/v1" -Model "deepseek-chat" ^
#     -ApiKey "sk-xxxx"
#
# What it does:
#   1. Copy app files to InstallDir (default D:\PolitePuppy\app)
#   2. Locate Python 3.10+ (no pip packages needed - stdlib only)
#   3. Write app\.env with your settings, lock down ACL
#   4. Create writable data dir (default D:\PolitePuppyData)
#   5. Start a verification instance and call /api/health
#   6. Print BT-panel process-guard config for keep-alive
# ============================================================
param(
    [string]$InstallDir  = "D:\PolitePuppy",
    [string]$DataDir     = "D:\PolitePuppyData",
    [int]   $Port        = 8765,
    [string]$Bind        = "127.0.0.1",
    [string]$AllowedHosts = "",
    [string]$BaseUrl     = "https://api.deepseek.com/v1",
    [string]$Model       = "deepseek-chat",
    [string]$ApiKey      = ""
)

$ErrorActionPreference = "Stop"
$AppSrc   = Join-Path $PSScriptRoot "app"
$AppDst   = Join-Path $InstallDir "app"

Write-Host "==== [1/6] copy app files ===="
if (-not (Test-Path (Join-Path $AppSrc "main.py"))) {
    throw "app\main.py not found next to deploy.ps1. Extract the full zip first."
}
New-Item -ItemType Directory -Path $AppDst -Force | Out-Null
Copy-Item -Path (Join-Path $AppSrc "*") -Destination $AppDst -Recurse -Force
Write-Host "    installed to: $AppDst"

Write-Host "==== [2/6] locate Python >= 3.10 ===="
$pyCmd = $null
foreach ($cand in @("python", "py")) {
    try {
        $v = & $cand --version 2>$null
        if ($v -match "Python 3\.(\d+)") {
            if ([int]$Matches[1] -ge 10) { $pyCmd = $cand; break }
            Write-Host "    $cand is $v (need >= 3.10), skipping"
        }
    } catch { }
}
if (-not $pyCmd) {
    throw "Python 3.10+ not found. Install from https://www.python.org/downloads/ and tick 'Add python.exe to PATH', then re-run."
}
$vstr = & $pyCmd --version 2>$null
Write-Host "    using: $pyCmd ($vstr)"

Write-Host "==== [3/6] write app\.env ===="
$envPath = Join-Path $AppDst ".env"
if (-not $ApiKey -and (Test-Path $envPath)) {
    # keep existing key on re-run
    $old = Get-Content $envPath -Encoding UTF8 | Where-Object { $_ -match "^OLLAMA_API_KEY=" }
    if ($old) { $ApiKey = ($old -split "=", 2)[1].Trim() ; Write-Host "    keeping existing OLLAMA_API_KEY" }
}
$envLines = @(
    "POLITE_PUPPY_SERVER=1",
    "POLITE_PUPPY_BIND=$Bind",
    "POLITE_PUPPY_PORT=$Port",
    "POLITE_PUPPY_ALLOWED_HOSTS=$AllowedHosts",
    "POLITE_PUPPY_DATA=$DataDir",
    "OLLAMA_BASE_URL=$BaseUrl",
    "OLLAMA_MODEL=$Model",
    "OLLAMA_API_KEY=$ApiKey"
)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($envPath, $envLines, $utf8NoBom)
# lock ACL: SYSTEM + Administrators + current user (keeps service account readable)
# NOTE: do NOT use /inheritance:r alone - it can cut off the very user the
# process guard runs as, making main.py silently fall back to defaults.
icacls $envPath /inheritance:r /grant:r "SYSTEM:F" /grant:r "Administrators:F" /grant:r "${env:USERNAME}:F" | Out-Null
Write-Host "    written: $envPath (ACL locked: SYSTEM/Administrators/$env:USERNAME)"

Write-Host "==== [4/6] data dir ===="
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
Write-Host "    ready: $DataDir"

Write-Host "==== [5/6] verification run ===="
$proc = Start-Process $pyCmd -ArgumentList "main.py","--server" -WorkingDirectory $AppDst -PassThru -WindowStyle Hidden
$ok = $false
$health = ""
function Get-Health([int]$p) {
    # direct connection, never via system proxy
    $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$p/api/health")
    $req.Proxy = $null
    $req.Host = "127.0.0.1:$p"
    $req.Timeout = 3000
    try {
        $resp = $req.GetResponse()
        $sr = New-Object IO.StreamReader($resp.GetResponseStream())
        $body = $sr.ReadToEnd()
        $sr.Close()
        $resp.Close()
        return $body
    } catch { return "" }
}
foreach ($i in 1..15) {
    Start-Sleep -Seconds 2
    if ($proc.HasExited) { break }
    $health = Get-Health $Port
    if ($health) { $ok = $true; break }
}
if ($ok) {
    Write-Host "    SERVICE UP: $health"
    if ($health -match '"ok":\s*false') {
        Write-Host "    WARNING: HTTP service is up but the LLM backend is unreachable."
        Write-Host "             Check OLLAMA_API_KEY / OLLAMA_BASE_URL / OLLAMA_MODEL in $envPath"
        Write-Host "             (health 'ok' becomes true once the model answers)."
    } else {
        Write-Host "    LLM backend reachable - all green."
    }
    Write-Host "    verification PASSED (pid $($proc.Id)) - leaving it running"
} else {
    Write-Host "    verification FAILED - check log:"
    $log = Join-Path $env:APPDATA "PolitePuppyPet\pet.log"
    if (Test-Path $log) { Get-Content $log -Tail 15 | ForEach-Object { Write-Host "      $_" } }
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    throw "Server did not become healthy. Fix the issue above and re-run deploy.ps1."
}

Write-Host "==== [6/6] keep-alive (BT panel process guard) ===="
Write-Host @"
    The test instance is RUNNING now (pid $($proc.Id)). Two options:

    A) Keep it as-is (no auto-restart). Not recommended.

    B) BT panel keep-alive (recommended):
       1. Stop this test instance first:
          taskkill /PID $($proc.Id) /F
       2. BT panel -> Software Store -> install process guard plugin
       3. Add a task:
          Name:     PolitePuppy
          Run user: Administrator
          Process:  $pyCmd
          Args:     main.py --server
          WorkDir:  $AppDst
          Env vars: (none needed - all in app\.env)
       4. Restart-on-crash: 3 retries, interval 10s (plugin default)

    Then reverse-proxy your domain to http://127.0.0.1:$Port
    (must send Host = `$host, disable proxy buffering for SSE).
    Full guide: see 部署指南.md in this package.
"@
Write-Host "==== DEPLOY DONE ===="
