@echo off
setlocal
cd /d "%~dp0"

rem try portable env -> .venv -> venv -> python from PATH
set "PY="
for /d %%D in ("%~dp0..\comfyui*") do if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
if not defined PY if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0..\venv\Scripts\python.exe" set "PY=%~dp0..\venv\Scripts\python.exe"
if not defined PY for %%P in (python.exe) do if not "%%~$PATH:P"=="" set "PY=%%~$PATH:P"
if not defined PY goto nopy
if not exist "%PY%" goto nopy

title Pixel Sprite Studio
echo Starting web UI ...
echo.
"%PY%" -s "%~dp0webui.py"
set "RC=%errorlevel%"
if "%RC%"=="2" exit /b 0
echo.
if not "%RC%"=="0" echo [ERROR] webui.py exited with code %RC%
pause
exit /b 0

:nopy
echo [ERROR] Python not found.
echo Looked for: ..\comfyui*\python.exe , ..\.venv , ..\venv , PATH
echo.
pause
exit /b 1
