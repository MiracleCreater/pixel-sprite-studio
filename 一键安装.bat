@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Pixel Sprite Studio - one click install

where python >nul 2>nul
if errorlevel 1 goto nopy
python -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 goto oldpy

echo ============================================================
echo   Pixel Sprite Studio - one click install
echo ============================================================
echo   [1] clone ComfyUI v0.42.0 + install pixel_memory node
echo   [2] install python deps (CUDA torch + ComfyUI requirements)
echo   [3] download models  (about 64 GB, resumable)
echo   [4] link models into ComfyUI
echo ============================================================
echo.
echo  Requires: NVIDIA GPU with 8GB+ VRAM, driver for CUDA 12.8+
echo  Free disk: about 80 GB
echo.
set "GO=Y"
set /p GO=Download models now (about 64 GB)? [Y/n]:
echo.
if /i "%GO%"=="n" goto nodl

python install.py --install-torch --download-models --link-models
goto finish

:nodl
python install.py --install-torch --link-models
echo.
echo Skipped model download. Run download_models.bat later to get them.

:finish
echo.
echo ============================================================
if errorlevel 1 echo [WARN] something failed, see messages above
echo  Done. Next: double click  Start-WebUI.bat
echo  Then open http://127.0.0.1:8899
echo ============================================================
echo.
pause
exit /b 0

:nopy
echo [ERROR] Python not found in PATH.
echo Install Python 3.10-3.12 from https://www.python.org/downloads/
echo Remember to check "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:oldpy
echo [ERROR] Python 3.10 or newer is required.
echo Current version:
python -V
echo.
pause
exit /b 1
