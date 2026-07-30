@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
if not exist "runtime_logs" mkdir "runtime_logs"
if not exist "outputs" mkdir "outputs"
set "PORT=8765"
set "EXPECTED_VERSION=V8.35"
set "URL=http://127.0.0.1:%PORT%/?v=8.35"
set "LOG=runtime_logs\BFXPS_AI_WEB_SERVER.log"
echo MODE=STRICT_ROOTLESS
echo ROOT=%CD%
echo FORWARD_DB=outputs\BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv
echo LAST3_DB=outputs\BEST_ENGINE_CHART_LAST3_TRADES.tsv
echo HISTORY_30N_DB=outputs\BEST_ENGINE_RECENT_TRADES.tsv
echo OHLC_OPTIONAL=outputs\VN30F1M_latest.csv
echo WARNING=bfxps_ai\config\backtested_warning_catalog.json
echo POLICY=bfxps_ai\config\advisor_policy.json
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do taskkill /PID %%P /F >nul 2>nul
set "PY_CMD="
where py >nul 2>nul
if !errorlevel! EQU 0 set "PY_CMD=py -3"
if not defined PY_CMD (
  where python >nul 2>nul
  if !errorlevel! EQU 0 set "PY_CMD=python"
)
if not defined PY_CMD (
  echo ERROR: Python was not found in PATH.
  pause
  exit /b 1
)
%PY_CMD% -c "import pandas, matplotlib" >nul 2>nul
if not !errorlevel! EQU 0 (
  echo ERROR: Missing Python packages pandas or matplotlib.
  echo Run: %PY_CMD% -m pip install pandas matplotlib
  pause
  exit /b 1
)
> "%LOG%" echo Starting BFXPS Smart Advisor %EXPECTED_VERSION%+++ Python Dynamic Charts 30D...
start "BFXPS Smart Advisor V8.35 Server" /MIN cmd /c call "%CD%\RUN_BFXPS_AI_SERVER.bat"
for /L %%I in (1,1,60) do (
  powershell -NoProfile -Command "try { $r=Invoke-RestMethod -UseBasicParsing 'http://127.0.0.1:%PORT%/health?ts=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(); if ($r.version -eq '%EXPECTED_VERSION%') { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>nul
  if !errorlevel! EQU 0 goto :OPEN_BROWSER
  timeout /t 1 /nobreak >nul
)
echo ERROR: %EXPECTED_VERSION% strict-rootless server did not start on port %PORT%.
echo See log: %CD%\%LOG%
echo Manual command: %PY_CMD% bfxps_ai\bfxps_server.py --port %PORT%
pause
exit /b 1
:OPEN_BROWSER
start "" "%URL%"
echo BFXPS Smart Advisor %EXPECTED_VERSION%+++ is running at %URL%
echo The server remains active after this launcher window closes.
echo Server log: %CD%\%LOG%
endlocal
