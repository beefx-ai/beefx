@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=%CD%\bfxps_ai
python bfxps_ai\bfxps_database_query_selftest.py
pause
