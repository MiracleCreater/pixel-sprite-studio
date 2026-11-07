@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Pixel Sprite Studio - one click install
chcp 65001 >nul 2>&1

where python >nul 2>nul
if errorlevel 1 goto nopy
python -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 goto oldpy

echo ============================================================
echo   Pixel Sprite Studio - one click install
echo ============================================================
echo   [1] check this PC (what it can run)
echo   [2] clone ComfyUI v0.42.0 + install pixel_memory node
echo   [3] install python deps (CUDA torch + ComfyUI requirements)
echo   [4] download models by tier (resumable)
echo   [5] link models into ComfyUI
echo ============================================================
echo.

echo ------------------------------------------------------------
echo   Step 1: hardware check
echo ------------------------------------------------------------
python -s "MyPixelTool\check_env.py"
echo.

echo ------------------------------------------------------------
echo   Step 2: choose model set
echo ------------------------------------------------------------
echo   [1] lite  about 15 GB   static pixel images only  (4-6 GB VRAM)
echo   [2] full  about 69 GB   images + animations       (8 GB+ VRAM)
echo   [3] auto  decide by my GPU (recommended)
echo.
echo   You can add the other set later by running download_models.bat again.
echo.
choice /c 123 /n /m "Your choice [1/2/3]: "
if errorlevel 3 set "TIER=auto"
if errorlevel 2 if not errorlevel 3 set "TIER=full"
if errorlevel 1 if not errorlevel 2 set "TIER=lite"
echo.
echo Selected: %TIER%
echo.
echo ------------------------------------------------------------
echo   Step 3: install (this takes a while, keep the window open)
echo ------------------------------------------------------------
echo.

python install.py --install-torch --download-models --link-models --tier %TIER%
set "RC=%errorlevel%"

echo.
echo ============================================================
if not "%RC%"=="0" echo  [WARN] something failed, see messages above.
echo  Done. Next:
echo    - local use : MyPixelTool folder, the "start web UI" bat file
echo    - share LAN : MyPixelTool folder, the "LAN server" bat file
echo  Then open  http://127.0.0.1:8899
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
