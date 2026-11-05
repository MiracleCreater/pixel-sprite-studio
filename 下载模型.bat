@echo off
setlocal
cd /d "%~dp0"
title Pixel Sprite Studio - download models

where python >nul 2>nul
if errorlevel 1 goto nopy

echo Checks and downloads the model files listed in models\manifest.json
echo Tip: add  --check  to only check, add  --only vae  to fetch part of them.
echo.
python download_models.py %*
set RC=%errorlevel%
echo.
if not "%RC%"=="0" echo [WARN] some files failed. Run this again to resume.
pause
exit /b %RC%

:nopy
echo [ERROR] Python not found in PATH.
pause
exit /b 1
