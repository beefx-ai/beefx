@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 bfxps_ai\bfxps_chat.py %*
) else (
  python bfxps_ai\bfxps_chat.py %*
)
endlocal
