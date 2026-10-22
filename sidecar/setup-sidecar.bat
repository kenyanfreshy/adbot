@echo off
REM One-time setup for the read-only trading -> AdBot bridge.
REM Its Python environment lives in %LOCALAPPDATA%\adbot-sidecar (outside OneDrive, so syncing doesn't slow it down).
REM Nothing in fx_trading_system is touched.
setlocal
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\adbot-sidecar\.venv"
echo.
echo === AdBot trading bridge setup ===
echo.
echo [1/4] Checking Python...
py -3 --version || (echo Python 3 not found. Install it from python.org, then run this again. & pause & exit /b 1)
if not exist "%VENV%\Scripts\python.exe" (
  echo [2/4] Creating its own Python environment in %VENV% ...
  py -3 -m venv "%VENV%" || (echo Could not create the environment. & pause & exit /b 1)
) else (
  echo [2/4] Python environment already exists.
)
echo [3/4] Installing psycopg2-binary and duckdb 1.5.6 (first time can take a few minutes)...
"%VENV%\Scripts\python" -m pip install --disable-pip-version-check --progress-bar on psycopg2-binary "duckdb==1.5.6" || (echo Install failed - see the message above. & pause & exit /b 1)
if "%MOTHERDUCK_TOKEN%"=="" (
  echo.
  echo Paste your MotherDuck token (the same one you gave Render^) and press Enter.
  set /p MD=Token: 
  call setx MOTHERDUCK_TOKEN "%%MD%%" >nul
  call set MOTHERDUCK_TOKEN=%%MD%%
)
echo.
echo [4/4] Running one test pass against the trading database...
"%VENV%\Scripts\python" trading_sidecar.py --trading-dir "%USERPROFILE%\OneDrive\Desktop\fx_trading_system" --once
echo.
echo If you see "pass ok" above, run start-sidecar.bat to keep it running.
pause
