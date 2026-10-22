@echo off
REM Keeps the read-only bridge running (one pass a minute). Close this window to stop it.
cd /d "%~dp0"
.venv\Scripts\python trading_sidecar.py --trading-dir "%USERPROFILE%\OneDrive\Desktop\fx_trading_system" --interval 60
