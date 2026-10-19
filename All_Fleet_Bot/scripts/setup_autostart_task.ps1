# ==============================================================================
# UNIFIED QUANT FLEET - WINDOWS AUTO-START CONFIGURATION SCRIPT
# Configures the Quant Fleet to automatically boot up after system restarts,
# power outages, weekend breaks, and market holidays.
# ==============================================================================

[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"

$workspaceRoot = (Get-Item "$PSScriptRoot\..").FullName
$batFile = Join-Path $workspaceRoot "start_fleet.bat"
$taskName = "UnifiedQuantFleet_AutoStart"

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host " 🚀 UNIFIED QUANT FLEET - CAU HINH TU DONG KHOI DONG HE THONG (24/7)" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host " Workspace: $workspaceRoot"
Write-Host " Target Batch: $batFile"
Write-Host ""

if (-not (Test-Path $batFile)) {
    Write-Error "Khong tim thay file $batFile!"
    exit 1
}

# 1. Register Windows Scheduled Task
Write-Host "👉 [1/2] Dang dang ky Windows Scheduled Task [$taskName]..." -ForegroundColor Yellow

try {
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batFile`"" -WorkingDirectory $workspaceRoot
    $triggerLogon = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 365) -MultipleInstancesPolicy IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

    # Unregister existing if present
    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false

    # Register new task
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggerLogon -Settings $settings -Description "Tu dong khoi dong Unified Quant Fleet 5 Bots sau khoi dong may / nghi le / cuoi tuan." | Out-Null
    Write-Host "   [OK] Scheduled Task da dang ky thanh cong!" -ForegroundColor Green
}
catch {
    Write-Warning "   [!] Khong the tao Scheduled Task qua API: $_. Su dung phuong thuc Startup Folder ben duoi."
}

# 2. Create Windows Startup Folder Shortcut (Universal Fallback)
Write-Host "👉 [2/2] Dang tao Shortcut tai thu muc Windows Startup..." -ForegroundColor Yellow
$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder "UnifiedQuantFleet.lnk"

try {
    $wshShell = New-Object -ComObject WScript.Shell
    $shortcut = $wshShell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = "cmd.exe"
    $shortcut.Arguments = "/c `"$batFile`""
    $shortcut.WorkingDirectory = $workspaceRoot
    $shortcut.Description = "Khoi dong Unified Quant Fleet 5 Bots"
    $shortcut.WindowStyle = 1
    $shortcut.Save()

    Write-Host "   [OK] Da tao Shortcut thanh cong tai: $shortcutPath" -ForegroundColor Green
}
catch {
    Write-Warning "   [!] Khong the tao Shortcut: $_"
}

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host " CAU HINH HOAN TAT!" -ForegroundColor Green
Write-Host " He thong se tu dong khoi dong MT5 va Unified Quant Fleet moi khi:"
Write-Host "   1. May tinh khoi dong lai sau cup dien hoac Windows Update."
Write-Host "   2. Dang nhap Windows vao dau tuan moi (Sunday 21:00 UTC)."
Write-Host "   3. Bot van tu dong standby tiet kiem CPU trong ky nghi le / cuoi tuan."
Write-Host "======================================================================" -ForegroundColor Cyan
