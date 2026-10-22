@echo off
REM Keeps the read-only bridge running (one pass a minute). Close this window to stop it.
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\adbot-sidecar\.venv"
if not exist "%VENV%\Scripts\python.exe" (echo Run setup-sidecar.bat first. & pause & exit /b 1)
echo AdBot trading bridge running - leave this window open. Close it to stop.
"%VENV%\Scripts\python" trading_sidecar.py --trading-dir "%USERPROFILE%\OneDrive\Desktop\fx_trading_system" --interval 60
pause
