@echo off
setlocal
cd /d "%~dp0"
title Pixel Sprite Studio - download models

where python >nul 2>nul
if errorlevel 1 goto nopy

rem Pass arguments through when given:  download_models.bat --tier lite --check
if not "%~1"=="" goto run

echo ============================================================
echo   Choose a model set
echo ============================================================
echo   [1] lite  about 15 GB   static pixel images only
echo       Works on 4-6 GB VRAM cards. No animation.
echo   [2] full  about 69 GB   images + animation
echo       Needs 8 GB VRAM and 32 GB system RAM.
echo   [3] auto  detect my GPU and pick one (recommended)
echo ============================================================
echo.
choice /c 123 /n /m "Your choice [1/2/3]: "
if errorlevel 3 set "TIER=auto"
if errorlevel 2 if not errorlevel 3 set "TIER=full"
if errorlevel 1 if not errorlevel 2 set "TIER=lite"
echo.
echo Tier: %TIER%
echo.
set "ARGS=--tier %TIER%"
goto go

:run
set "ARGS=%*"

:go
python download_models.py %ARGS%
set RC=%errorlevel%
echo.
if not "%RC%"=="0" echo [WARN] some files failed. Run this again to resume.
pause
exit /b %RC%

:nopy
echo [ERROR] Python not found in PATH.
pause
exit /b 1
