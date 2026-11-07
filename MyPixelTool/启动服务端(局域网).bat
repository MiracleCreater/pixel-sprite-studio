@echo off
setlocal
cd /d "%~dp0"
title Pixel Sprite Studio - LAN Server

rem find python: portable env -> .venv -> venv -> PATH
set "PY="
for /d %%D in ("%~dp0..\comfyui*") do if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
if not defined PY if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0..\venv\Scripts\python.exe" set "PY=%~dp0..\venv\Scripts\python.exe"
if not defined PY for %%P in (python.exe) do if not "%%~$PATH:P"=="" set "PY=%%~$PATH:P"
if not defined PY goto nopy
if not exist "%PY%" goto nopy

echo.
echo  Starting LAN server. Other devices on the same WiFi can use it in a browser.
echo  Keep this window open. Press Ctrl+C or close it to stop.
echo.
"%PY%" -s "%~dp0webui.py" --lan
set "RC=%errorlevel%"
if "%RC%"=="2" exit /b 0
echo.
pause
exit /b %RC%

:nopy
echo.
echo  [ERROR] Python not found. Please run the installer first.
echo.
pause
exit /b 1
