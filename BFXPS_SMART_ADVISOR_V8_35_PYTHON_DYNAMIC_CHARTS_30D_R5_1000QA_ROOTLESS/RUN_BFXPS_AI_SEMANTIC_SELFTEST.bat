@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=%CD%\bfxps_ai
python bfxps_ai\bfxps_semantic_selftest.py
if errorlevel 1 pause
endlocal
