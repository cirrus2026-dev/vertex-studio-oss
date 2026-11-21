@echo off
chcp 65001 >nul
title Vertex AI Cloud Studio OSS - Launcher
cd /d "%~dp0"

echo ============================================================
echo   Vertex AI Cloud Studio (Open Source Edition) - Starting...
echo ============================================================
echo.

:: 1. ตรวจสอบคำสั่ง Python (รองรับทั้ง python และ py)
set "PY_CMD="
where python >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=python"
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set "PY_CMD=py -3"
    )
)

if "%PY_CMD%"=="" (
    echo [ERROR] ไม่พบโปรแกรม Python ในเครื่องนี้!
    echo กรุณาดาวน์โหลดและติดตั้ง Python 3.10+ จาก https://www.python.org/downloads/
    echo ^(อย่าลืมติ๊กถูก "Add Python to PATH" ตอนติดตั้ง^)
    pause
    exit /b 1
)

echo [1/4] ตรวจสอบ Python และติดตั้ง Dependencies จาก requirements.txt...
%PY_CMD% --version
if exist "requirements.txt" (
    %PY_CMD% -m pip install --disable-pip-version-check -r requirements.txt
) else (
    %PY_CMD% -m pip install --disable-pip-version-check websockets google-auth requests
)

:: สร้างไฟล์ .env เริ่มต้นอัตโนมัติหากยังไม่มี
if not exist ".env" (
    echo # วาง Google AI Studio API Key ฟรี ^(รับได้ที่ https://aistudio.google.com/apikey^)> ".env"
    echo GEMINI_API_KEY=>> ".env"
    echo OSS_PORT=5050>> ".env"
)

echo.
echo [2/4] กำลังเปิด Gemini Web Bridge Server (พอร์ต 8765)...
netstat -ano | findstr ":8765 " | findstr "LISTENING" >nul 2>nul
if %errorlevel%==0 (
    echo   - พอร์ต 8765 กำลังทำงานอยู่แล้ว ^(ข้ามการเปิดซ้ำ^)
) else (
    if exist "gemini_bridge_server.py" (
        start "Gemini Web Bridge (8765)" /min cmd /c "%PY_CMD% gemini_bridge_server.py"
    ) else (
        echo   - [ข้าม] ไม่พบไฟล์ gemini_bridge_server.py ในโฟลเดอร์นี้
    )
)

echo.
echo [3/4] กำลังเปิด Vertex AI Studio Server (พอร์ต 5050)...
netstat -ano | findstr ":5050 " | findstr "LISTENING" >nul 2>nul
if %errorlevel%==0 (
    echo   - พอร์ต 5050 กำลังทำงานอยู่แล้ว ^(ข้ามการเปิดซ้ำ^)
) else (
    set "OSS_PORT=5050"
    start "Vertex AI Studio OSS (5050)" /min cmd /c "%PY_CMD% server.py"
)

echo.
echo [4/4] รอเซิร์ฟเวอร์พร้อมทำงานและเปิดเบราว์เซอร์อัตโนมัติ...
timeout /t 2 /nobreak >nul
start "" "http://localhost:5050"

echo.
echo ============================================================
echo   พร้อมใช้งานแล้ว! เปิดหน้าต่าง: http://localhost:5050
echo   หากเปิดครั้งแรก กดปุ่ม "⚙️ ตั้งค่าการเชื่อมต่อ" มุมขวาบนเพื่อใส่ Gemini Key หรือสลับโหมด
echo ============================================================
echo.
pause
