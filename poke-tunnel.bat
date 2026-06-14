@echo off
REM Exposes Beeper Desktop's local MCP to Poke so Poke can read your chats and
REM draft replies. Requires the Poke CLI on PATH (npm i -g poke, then `poke login`).
REM The MCP endpoint/port may differ per Beeper version -- override with %POKE_MCP_URL%.
cd /d "%~dp0"
if "%POKE_MCP_URL%"=="" set "POKE_MCP_URL=http://localhost:23375/v0/mcp"
echo [%date% %time%] START poke tunnel >> poke-tunnel.log
call poke tunnel "%POKE_MCP_URL%" -n "Beeper Desktop" >> poke-tunnel.log 2>&1
echo [%date% %time%] EXIT code %errorlevel% >> poke-tunnel.log
