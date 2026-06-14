# Watchdog for the Beeper -> Poke bridge.
# Runs every minute (via the PokeBridge scheduled task). Relaunches the bridge
# if its heartbeat is stale or missing. The bridge's single-instance lock makes
# redundant launches harmless, so this is safe to run as often as you like.
$ErrorActionPreference = 'SilentlyContinue'
Set-Location -Path $PSScriptRoot

$hbFile = Join-Path $PSScriptRoot '.bridge-heartbeat'
$logFile = Join-Path $PSScriptRoot 'watchdog.log'
$now = [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

$age = 999999
if (Test-Path $hbFile) {
    $val = (Get-Content $hbFile -Raw).Trim()
    if ($val -match '^\d+$') { $age = $now - [int64]$val }
}

# Healthy: heartbeat written within the last 90s (interval is 30s).
if ($age -lt 90) { exit 0 }

$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*bridge.py*' })

# Stale heartbeat but a bridge process exists and it's been < 5 min: it's almost
# certainly still cold-starting (dependency import scan). Give it time.
if ($procs.Count -gt 0 -and $age -lt 300) {
    Add-Content $logFile "$(Get-Date -Format s) heartbeat stale ${age}s, bridge process present - waiting for startup"
    exit 0
}

# Dead, or hung past 5 min: clean up any stragglers and relaunch.
if ($procs.Count -gt 0) {
    Add-Content $logFile "$(Get-Date -Format s) heartbeat stale ${age}s, killing hung bridge ($($procs.Count) proc)"
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 2
}

Add-Content $logFile "$(Get-Date -Format s) relaunching bridge (heartbeat age ${age}s)"
Start-Process -FilePath 'wscript.exe' `
    -ArgumentList ('"' + (Join-Path $PSScriptRoot 'run-bridge.vbs') + '"') `
    -WindowStyle Hidden
exit 0
