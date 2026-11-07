@echo off
setlocal
cd /d "%~dp0"
title Pixel Sprite Studio - Hardware Check
chcp 65001 >nul 2>&1

set "PY="
for /d %%D in ("%~dp0..\comfyui*") do if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
if not defined PY if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0..\venv\Scripts\python.exe" set "PY=%~dp0..\venv\Scripts\python.exe"
if not defined PY for %%P in (python.exe) do if not "%%~$PATH:P"=="" set "PY=%%~$PATH:P"
if not defined PY goto nopy

"%PY%" -s "%~dp0check_env.py"
echo.
pause
exit /b 0

:nopy
echo.
echo  [ERROR] Python not found. Please run the installer first.
echo.
pause
exit /b 1
