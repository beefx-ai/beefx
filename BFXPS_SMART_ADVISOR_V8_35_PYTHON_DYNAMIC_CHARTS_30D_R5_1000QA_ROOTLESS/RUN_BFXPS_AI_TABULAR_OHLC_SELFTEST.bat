@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%CD%\bfxps_ai"
python bfxps_ai\bfxps_tabular_ohlc_selftest.py
endlocal
