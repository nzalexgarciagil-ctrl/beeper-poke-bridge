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

$hbExists = Test-Path $hbFile
$age = 999999
if ($hbExists) {
    $val = (Get-Content $hbFile -Raw).Trim()
    if ($val -match '^\d+$') { $age = $now - [int64]$val }
}

# Healthy: heartbeat written within the last 90s (interval is 30s).
if ($age -lt 90) { exit 0 }

$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*bridge.py*' })

# A bridge process exists. Two cases:
#   - No heartbeat file yet, or it's < 5 min old: the bridge is still cold-starting
#     (uv resolve + Defender scan can take a minute or two). Leave it ALONE -- do
#     not kill a process that simply hasn't reached its first heartbeat yet.
#   - Heartbeat exists AND is older than 5 min: genuinely hung -> kill & relaunch.
if ($procs.Count -gt 0) {
    if ($hbExists -and $age -gt 300) {
        Add-Content $logFile "$(Get-Date -Format s) heartbeat hung at ${age}s, killing ($($procs.Count) proc)"
        $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Start-Sleep -Seconds 2
    } else {
        Add-Content $logFile "$(Get-Date -Format s) bridge process present (hb age ${age}s) - waiting for startup"
        exit 0
    }
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
