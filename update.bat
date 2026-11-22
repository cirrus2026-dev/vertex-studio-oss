@echo off
chcp 65001 >nul
echo ===========================================================
echo   Vertex Studio OSS - Auto Updater
echo ===========================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Update failed. Please check the message above.
    pause
    exit /b %errorlevel%
)
echo.
echo [OK] Update complete! Please restart gemini_bridge_server.py if it is running.
pause
