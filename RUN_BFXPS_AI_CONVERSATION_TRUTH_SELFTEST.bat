@echo off
cd /d %~dp0
set PYTHONPATH=bfxps_ai
python bfxps_ai\bfxps_conversation_truth_selftest.py
pause
