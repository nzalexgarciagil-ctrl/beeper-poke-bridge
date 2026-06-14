# Running the bridge on Windows (always-on)

Beeper Desktop must be running for the bridge to connect.

## Recommended: heartbeat watchdog (survives silent death)

The bridge runs detached (no console window) via `run-bridge.vbs`. Because it
detaches, Task Scheduler's built-in *restart on failure* does **not** work — the
task "completes" the instant it launches, so it never notices the bridge dying
later (sleep, logoff, OOM, network).

Instead, a tiny watchdog (`watchdog.ps1`) runs **every minute** and relaunches
the bridge whenever its `.bridge-heartbeat` file goes stale. The bridge's
single-instance lock makes redundant launches harmless, so this is safe to run
as often as you like. Recovery is automatic within ~1 minute of any death.

Register it (one line, no admin needed):

```bat
schtasks /Create /TN PokeBridge ^
  /TR "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\path\to\poke\watchdog.ps1" ^
  /SC MINUTE /MO 1 /RU %USERNAME% /IT /RL LIMITED /F
```

- `/SC MINUTE /MO 1` — run every minute.
- `/IT` — run in your interactive session (needed so the bridge it spawns can
  reach Beeper Desktop's local API).
- The watchdog also starts the bridge within a minute of logon, so you don't
  need a separate at-logon task.

Check it's working:

```bat
schtasks /Query /TN PokeBridge        REM should show Ready / Next Run
type watchdog.log                     REM relaunch decisions
type bridge.log                       REM the bridge's own rotating log
```

To pause/stop supervision: `schtasks /Change /TN PokeBridge /DISABLE`
(and kill the running python if you want it down immediately).

## Cold-start note (Windows Defender)

The first launch after a reboot or after the dependency cache changes can take
30–120s because Defender real-time-scans the Python packages on first import.
Once scanned they're cached and startup drops to ~10s. The watchdog tolerates
this — it waits up to 5 minutes for a starting process before forcing a
relaunch. If you want fast cold starts, add a Defender exclusion for the uv
cache (`%LOCALAPPDATA%\uv`) — this needs an elevated PowerShell:

```powershell
Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\uv"
```

## Alternative: NSSM (run as a service)

[NSSM](https://nssm.cc) can run the bridge as a service with restart, but
services run in session 0 and usually can't see Beeper Desktop's local API. The
watchdog approach above is more reliable for a desktop-app dependency.
