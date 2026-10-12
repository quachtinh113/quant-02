@echo off
title UNIFIED QUANT FLEET — 5 BOTS ON 1 PC (Master Watchdog)
color 0B

echo =======================================================
echo    UNIFIED QUANT FLEET - 5 BOTS TREN 1 PC
echo    He Thong Giao Dich Dinh Luong Tu Dong 24/7
echo =======================================================
echo.

:: Ensure we are in the correct directory
cd /d "%~dp0"

:: Set utf-8 encoding for proper emoji display
set PYTHONIOENCODING=utf-8

:: Ensure Python is in PATH
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
)

:: Ensure MT5 Terminal is running and authorized
tasklist /fi "imagename eq terminal64.exe" 2>NUL | find /i /n "terminal64.exe">NUL
if "%ERRORLEVEL%"=="1" (
    echo [INFO] MT5 terminal chua mo. Dang tu dong khoi dong MT5 voi tai khoan 414337806...
    if exist "%~dp0mt5_startup.ini" (
        start "" "C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe" /config:"%~dp0mt5_startup.ini"
    ) else (
        start "" "C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
    )
    timeout /t 6 /nobreak > nul
)

:: Auto-restart loop (max 50 restarts to prevent infinite crash loops)
set /a RESTART_COUNT=0
set MAX_RESTARTS=50
set RESTART_DELAY=15

:start_loop
set /a RESTART_COUNT+=1

if %RESTART_COUNT% GTR %MAX_RESTARTS% (
    echo.
    echo =======================================================
    echo  ERROR: Da dat gioi han restart [%MAX_RESTARTS%].
    echo  Vui long kiem tra logs/fleet.log de xu ly.
    echo =======================================================
    pause > nul
    exit /b 1
)

echo.
echo [%date% %time%] Khoi dong Unified Quant Fleet (Lan thu %RESTART_COUNT%/%MAX_RESTARTS%)...
echo.

python run_unified_bot.py

:: Check exit code
set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% EQU 0 (
    echo.
    echo =======================================================
    echo  Fleet da dung an toan [exit code 0]. Khong tu restart.
    echo =======================================================
    pause > nul
    exit /b 0
)

echo.
echo =======================================================
echo  Bot gap su co (code: %EXIT_CODE%).
echo  Tu dong restart sau %RESTART_DELAY% giay...
echo  (Nhan Ctrl+C de huy restart)
echo =======================================================

:: Wait before restart (allows user to cancel)
timeout /t %RESTART_DELAY% /nobreak

goto start_loop
