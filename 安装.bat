@echo off
setlocal
cd /d "%~dp0"
title Pixel Sprite Studio - install
where python >nul 2>nul
if errorlevel 1 goto nopy
python install.py %*
echo.
pause
exit /b 0

:nopy
echo [ERROR] Python not found in PATH.
echo Install Python 3.10+ and make sure 'python' works in cmd.
echo.
pause
exit /b 1
