@echo off
set PYTHONPATH=%~dp0bfxps_ai
cd /d %~dp0
python bfxps_ai\bfxps_trader_execution_conversation_selftest.py
pause
