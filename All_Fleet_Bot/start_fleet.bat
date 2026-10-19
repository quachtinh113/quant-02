@echo off
title UNIFIED QUANT FLEET - 5 BOTS ON 1 PC (Master Watchdog)
color 0B

echo =======================================================
echo    UNIFIED QUANT FLEET - 5 BOTS TREN 1 PC
echo    He Thong Giao Dich Dinh Luong Tu Dong 24/7
echo    Co Che Tu Dong Khoi Dong va Phuc Hoi Sau Nghi Le
echo =======================================================
echo.

:: Ensure we are in the correct directory
cd /d "%~dp0"

:: Set utf-8 encoding for proper emoji display
set PYTHONIOENCODING=utf-8

:: Add Python and Git to PATH
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
)
if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" (
    set "PATH=%LOCALAPPDATA%\Programs\Git\cmd;%PATH%"
)

:: Wait for network connectivity after PC boot / restart (try up to 30s)
echo [1/3] Kiem tra ket noi mang Internet...
set NET_ATTEMPTS=0

:check_network
ping -n 1 8.8.8.8 >nul 2>&1
if not errorlevel 1 goto net_ok
set /a NET_ATTEMPTS+=1
if %NET_ATTEMPTS% GEQ 6 (
    echo [CANH BAO] Chua co ket noi Internet sau 30s. He thong van tiep tuc de MT5 tu ket noi...
    goto net_ok
)
echo       [INFO] Dang cho ket noi mang - thu lai lan %NET_ATTEMPTS%/6...
timeout /t 5 /nobreak > nul
goto check_network

:net_ok
echo       [OK] Ket noi mang san sang.

:: Ensure MT5 Terminal is running and authorized
echo [2/3] Kiem tra MetaTrader 5 Terminal...
tasklist /fi "imagename eq terminal64.exe" 2>NUL | find /i "terminal64.exe" >nul
if errorlevel 1 (
    echo [INFO] MT5 terminal chua mo. Dang tu dong khoi dong MT5 voi tai khoan 414337806...
    if exist "%~dp0mt5_startup.ini" (
        start "" "C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe" /config:"%~dp0mt5_startup.ini"
    ) else (
        start "" "C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
    )
    echo [INFO] Dang cho MT5 ket noi toi may chu Exness trong 10 giay...
    ping 127.0.0.1 -n 11 > nul
) else (
    echo       [OK] MetaTrader 5 dang chay.
)

:: Auto-restart loop
echo [3/3] Khoi chay Master Orchestrator...
set /a RESTART_COUNT=0
set MAX_RESTARTS=200
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
echo [%date% %time%] Khoi dong Unified Quant Fleet [Lan thu %RESTART_COUNT%/%MAX_RESTARTS%]...
echo.

python run_unified_bot.py

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
echo  Bot gap su co [code: %EXIT_CODE%].
echo  Tu dong restart sau %RESTART_DELAY% giay...
echo  Nhan Ctrl+C de huy restart
echo =======================================================

timeout /t %RESTART_DELAY% /nobreak
goto start_loop
