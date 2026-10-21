@echo off
:: ==============================================================================
:: UNIFIED QUANT FLEET - 1-CLICK ELEVATED AUTO-START INSTALLER
:: Registers Windows Scheduled Task with 'AtStartup' and 'AtLogOn' triggers
:: so the Fleet boots automatically upon system restart, power outage, or login.
:: ==============================================================================
setlocal EnableDelayedExpansion

title UNIFIED QUANT FLEET - AUTO-START CONFIGURATION (ADMIN)
color 0B

echo ======================================================================
echo  UNIFIED QUANT FLEET - CAU HINH TU DONG KHOI DONG HE THONG (24/7)
echo ======================================================================
echo.

:: Check for Administrator privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Yeu cau quyen Administrator de dang ky Windows Scheduled Task cap he thong...
    echo Dang mo hop thoai xac nhan UAC (User Account Control)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

:: Running as Administrator
cd /d "%~dp0\.."
set "WORKSPACE=%CD%"
set "TARGET_BAT=%WORKSPACE%\start_fleet.bat"
set "TASK_NAME=UnifiedQuantFleet_AutoStart"

echo  Thu muc goc: %WORKSPACE%
echo  File thuc thi: %TARGET_BAT%
echo.

if not exist "%TARGET_BAT%" (
    color 0C
    echo [LOI] Khong tim thay file %TARGET_BAT%!
    pause
    exit /b 1
)

:: 1. Register Scheduled Task via PowerShell
echo [1/2] Dang dang ky Windows Scheduled Task [%TASK_NAME%] (AtStartup + AtLogOn)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"\"%TARGET_BAT%\"\"' -WorkingDirectory '%WORKSPACE%';" ^
    "$triggerStartup = New-ScheduledTaskTrigger -AtStartup;" ^
    "$triggerLogon = New-ScheduledTaskTrigger -AtLogOn;" ^
    "$triggers = @($triggerStartup, $triggerLogon);" ^
    "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 365) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1);" ^
    "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest;" ^
    "Get-ScheduledTask -TaskName '%TASK_NAME%' -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false;" ^
    "Register-ScheduledTask -TaskName '%TASK_NAME%' -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'Tu dong khoi dong Unified Quant Fleet 5 Bots sau reboot / nghi le / cuoi tuan.' | Out-Null;" ^
    "Write-Host '   [OK] Scheduled Task da duoc dang ky thanh cong voi trigger AtStartup + AtLogOn!' -ForegroundColor Green"

if %errorlevel% neq 0 (
    echo.
    echo [THONG BAO] Thu dang ky Scheduled Task qua schtasks.exe...
    schtasks /create /tn "%TASK_NAME%" /tr "cmd.exe /c \"%TARGET_BAT%\"" /sc onstart /delay 0000:30 /rl highest /f >nul 2>&1
    if !errorlevel! equ 0 (
        echo    [OK] Dang ky qua schtasks /sc onstart thanh cong!
    ) else (
        echo    [!] Khong the dang ky qua schtasks. He thong van se su dung Startup Folder.
    )
)

:: 2. Create Windows Startup Folder Shortcut (Fallback)
echo.
echo [2/2] Tao Shortcut du phong trong thu muc Windows Startup...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$startupFolder = [Environment]::GetFolderPath('Startup');" ^
    "$shortcutPath = Join-Path $startupFolder 'UnifiedQuantFleet.lnk';" ^
    "$wshShell = New-Object -ComObject WScript.Shell;" ^
    "$shortcut = $wshShell.CreateShortcut($shortcutPath);" ^
    "$shortcut.TargetPath = 'cmd.exe';" ^
    "$shortcut.Arguments = '/c \"\"%TARGET_BAT%\"\"';" ^
    "$shortcut.WorkingDirectory = '%WORKSPACE%';" ^
    "$shortcut.Description = 'Khoi dong Unified Quant Fleet 5 Bots';" ^
    "$shortcut.WindowStyle = 1;" ^
    "$shortcut.Save();" ^
    "Write-Host '   [OK] Shortcut tai Windows Startup da san sang!' -ForegroundColor Green"

echo.
echo ======================================================================
echo  CAU HINH HOAN TAT!
echo  He thong se tu dong khoi dong MT5 va Unified Quant Fleet moi khi:
echo    1. May tinh khoi dong lai sau cup dien hoac Windows Update (AtStartup).
echo    2. Dang nhap vao may tinh / VPS (AtLogOn / Startup Shortcut).
echo    3. Bot tu dong standby trong phien nghi le va cuoi tuan.
echo ======================================================================
echo.
pause
