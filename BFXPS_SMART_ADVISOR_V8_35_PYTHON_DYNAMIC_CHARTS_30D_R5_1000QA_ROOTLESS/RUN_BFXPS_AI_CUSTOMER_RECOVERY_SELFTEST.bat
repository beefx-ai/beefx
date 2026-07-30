@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=%CD%\bfxps_ai
python bfxps_ai\bfxps_customer_recovery_selftest.py
pause
endlocal
