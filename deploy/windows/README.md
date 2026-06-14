# Running the bridge on Windows (always-on)

Beeper Desktop must be running for the bridge to connect.

## Option A — Task Scheduler with auto-restart (recommended, no extra tools)

1. Open **Task Scheduler** → **Create Task** (not "Basic Task").
2. **General**: name it `Poke Bridge`. Select *Run only when user is logged on*
   (Beeper Desktop's API only exists in your desktop session).
3. **Triggers**: *At log on* (your user).
4. **Actions**: *Start a program* →
   - Program: `wscript.exe`
   - Arguments: `"C:\path\to\poke\run-bridge.vbs"`
   (Using the `.vbs` keeps it windowless.)
5. **Settings**: tick *If the task fails, restart every* `1 minute`, up to `999`
   times, and untick *Stop the task if it runs longer than…*. This is what makes
   it survive the silent deaths.

To stop it: disable/end the task in Task Scheduler.

## Option B — NSSM (run as a service)

[NSSM](https://nssm.cc) wraps the bridge as a Windows service with auto-restart:

```
nssm install PokeBridge "C:\path\to\poke\run-bridge.bat"
nssm set PokeBridge AppDirectory "C:\path\to\poke"
nssm set PokeBridge AppExit Default Restart
nssm set PokeBridge AppRestartDelay 5000
nssm start PokeBridge
```

Note: services run in session 0 and may not see Beeper Desktop's local API.
Task Scheduler (Option A) is more reliable for a desktop app dependency.

## Liveness

The bridge writes `.bridge-heartbeat` (a unix timestamp) every 30s. If you want
an external check, a scheduled script can read it and restart the task if the
value is older than a couple of minutes. Task Scheduler's built-in restart-on-
failure already covers process death.
