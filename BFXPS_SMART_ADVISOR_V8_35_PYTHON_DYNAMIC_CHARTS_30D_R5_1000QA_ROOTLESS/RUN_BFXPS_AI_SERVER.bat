@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "runtime_logs" mkdir "runtime_logs"
if not exist "outputs" mkdir "outputs"
set "LOG=runtime_logs\BFXPS_AI_WEB_SERVER.log"
set "MPLCONFIGDIR=%CD%\runtime_logs\.matplotlib"
if not exist "%MPLCONFIGDIR%" mkdir "%MPLCONFIGDIR%"
set "PYTHONUNBUFFERED=1"
set "PY_CMD="
where py >nul 2>nul
if %errorlevel% EQU 0 set "PY_CMD=py -3"
if not defined PY_CMD (
  where python >nul 2>nul
  if %errorlevel% EQU 0 set "PY_CMD=python"
)
if not defined PY_CMD exit /b 1
%PY_CMD% "bfxps_ai\bfxps_server.py" --port 8765 >> "%LOG%" 2>&1
