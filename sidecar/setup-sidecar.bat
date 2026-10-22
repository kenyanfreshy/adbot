@echo off
REM One-time setup for the read-only trading -> AdBot bridge.
REM Creates its own Python environment here; does not touch fx_trading_system.
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || (echo Python 3 not found. Install it from python.org first. & pause & exit /b 1)
)
.venv\Scripts\python -m pip install --quiet --upgrade pip
.venv\Scripts\pip install --quiet psycopg2-binary duckdb || (echo Install failed. & pause & exit /b 1)
if "%MOTHERDUCK_TOKEN%"=="" (
  echo.
  echo Paste your MotherDuck token (the same one you gave Render) and press Enter.
  set /p MD=Token: 
  setx MOTHERDUCK_TOKEN "%MD%" >nul
  set MOTHERDUCK_TOKEN=%MD%
)
echo.
echo Running one test pass...
.venv\Scripts\python trading_sidecar.py --trading-dir "%USERPROFILE%\OneDrive\Desktop\fx_trading_system" --once
echo.
echo If you see "pass ok" above, open the Trading tab in AdBot. Then run start-sidecar.bat to keep it running.
pause
