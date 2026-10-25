@echo off
cd /d "%~dp0"
uv run python scripts/pipeline_app.py --open-browser %*
if errorlevel 1 pause
