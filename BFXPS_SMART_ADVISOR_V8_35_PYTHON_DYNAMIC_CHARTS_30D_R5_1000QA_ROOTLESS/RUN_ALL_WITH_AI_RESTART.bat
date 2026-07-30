@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "PORT=8765"
echo ============================================
echo BFXPS RUN_ALL + RESTART AI WEB V8.35+++
echo ROOT=%CD%
echo ============================================
echo [1/4] Stop old AI server on port %PORT%...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do taskkill /PID %%P /F >nul 2>nul
timeout /t 2 /nobreak >nul
if not exist "scripts\run_all.py" (
  echo [ERROR] scripts\run_all.py not found in %CD%.
  echo Copy this AI overlay into the real BFXPS root before using this BAT.
  pause
  exit /b 1
)
echo [2/4] Run pipeline and rebuild outputs...
py -3 scripts\run_all.py
if errorlevel 1 (
  echo [ERROR] RUN_ALL failed. AI web was not restarted.
  pause
  exit /b 1
)
echo [3/4] Start AI server from current root...
if not exist "runtime_logs" mkdir "runtime_logs"
start "BFXPS AI Server" /MIN cmd /c call "%CD%\RUN_BFXPS_AI_SERVER.bat"
echo [4/4] Wait for V8.35 health...
for /L %%I in (1,1,60) do (
  powershell -NoProfile -Command "try { $r=Invoke-RestMethod 'http://127.0.0.1:%PORT%/health' -TimeoutSec 2; if($r.ok -and $r.version -eq 'V8.35'){exit 0}else{exit 1} } catch { exit 1 }" >nul 2>nul
  if !errorlevel! EQU 0 goto :ONLINE
  timeout /t 1 /nobreak >nul
)
echo [ERROR] RUN_ALL succeeded but AI server did not become healthy.
echo Log: %CD%\runtime_logs\BFXPS_AI_WEB_SERVER.log
pause
exit /b 1
:ONLINE
echo [OK] Outputs rebuilt and AI web V8.35+++ is online.
echo Local:  http://127.0.0.1:%PORT%
echo Public: https://ai.beefx.com
pause
