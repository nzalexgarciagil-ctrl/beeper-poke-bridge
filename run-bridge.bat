@echo off
REM Run the Beeper -> Poke bridge via uv (deps resolved from requirements.txt and
REM cached under %LOCALAPPDATA%\uv, so startup is fast after the first run).
REM The app writes its own rotating bridge.log; this also captures launcher/uv
REM startup errors (before app logging) to bridge-launch.log.
cd /d "%~dp0"
echo [%date% %time%] launching bridge >> bridge-launch.log
uv run --with-requirements requirements.txt python bridge.py %* >> bridge-launch.log 2>&1
echo [%date% %time%] bridge exited with code %errorlevel% >> bridge-launch.log
