@echo off
REM Interactive setup: collects your credentials and writes .env.
cd /d "%~dp0"
uv run --with-requirements requirements.txt python configure.py
