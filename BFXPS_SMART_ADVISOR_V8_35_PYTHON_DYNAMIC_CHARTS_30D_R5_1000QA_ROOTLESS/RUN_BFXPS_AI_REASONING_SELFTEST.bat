@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=%CD%\bfxps_ai
python bfxps_ai\bfxps_reasoning_selftest.py
pause
