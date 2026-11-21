@echo off
chcp 65001 >nul
title AI Persistent Bridge Launcher (Port 8765)

echo ========================================================
echo   Launching Google Chrome (Dedicated AI Profile)
echo   Launching Persistent Bridge Server (Port 8765)
echo ========================================================

:: 1. Dedicated Chrome User Data Directory for AI Bridge
set CHROME_DATA_DIR=%USERPROFILE%\.gemini\AI-Chrome-Profile
if not exist "%CHROME_DATA_DIR%" mkdir "%CHROME_DATA_DIR%"

:: 2. Allow current workspace directory for safe code file attachment
set BRIDGE_EXTRA_UPLOAD_ROOTS=%CD%

:: 3. Launch Google Chrome with Remote Debugging enabled (Port 9222)
echo [1/2] Starting Google Chrome...
start "" "chrome.exe" ^
  --remote-debugging-port=9222 ^
  --remote-debugging-address=127.0.0.1 ^
  --user-data-dir="%CHROME_DATA_DIR%" ^
  --no-first-run ^
  --no-default-browser-check ^
  "https://gemini.google.com/app"

:: 4. Wait 2 seconds for Chrome CDP initialization
timeout /t 2 /nobreak >nul

:: 5. Start Persistent Bridge Server on Port 8765 in background
echo [2/2] Starting Bridge Server on Port 8765...
start /b "" pythonw "%~dp0gemini_bridge_server.py"

echo ========================================================
echo   Bridge Ready! Port 8765 and Chrome CDP 9222 are active.
echo ========================================================
timeout /t 3 >nul
exit
