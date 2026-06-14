# Windows launcher + watchdog for the Beeper -> Poke bridge.
#
# Run this every minute via Task Scheduler (see README "Keeping it running").
# It starts the bridge if it isn't running and relaunches it whenever the
# heartbeat goes stale. The bridge's single-instance lock makes redundant
# launches harmless, so running this often is safe. The bridge is started
# windowless (no console pops up).
$ErrorActionPreference = 'SilentlyContinue'
Set-Location -Path $PSScriptRoot

$hbFile  = Join-Path $PSScriptRoot '.bridge-heartbeat'
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

# Stale heartbeat but a bridge process exists and it's been < 5 min: almost
# certainly still cold-starting (dependency scan on first import). Give it time.
if ($procs.Count -gt 0 -and $age -lt 300) {
    Add-Content $logFile "$(Get-Date -Format s) heartbeat stale ${age}s, bridge process present - waiting for startup"
    exit 0
}

# Dead, or hung past 5 min: clean up any stragglers and relaunch windowless.
if ($procs.Count -gt 0) {
    Add-Content $logFile "$(Get-Date -Format s) heartbeat stale ${age}s, killing hung bridge ($($procs.Count) proc)"
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 2
}

Add-Content $logFile "$(Get-Date -Format s) launching bridge (heartbeat age ${age}s)"

# Launch with no console window. cmd handles the log redirect; uv resolves deps.
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName         = 'cmd.exe'
$psi.Arguments        = '/c uv run --with-requirements requirements.txt python bridge.py >> bridge-launch.log 2>&1'
$psi.WorkingDirectory = $PSScriptRoot
$psi.UseShellExecute  = $false
$psi.CreateNoWindow   = $true
[System.Diagnostics.Process]::Start($psi) | Out-Null
exit 0
