# ==============================================================================
# UNIFIED QUANT FLEET - REMOVE AUTO-START CONFIGURATION
# Removes Scheduled Task and Startup Shortcut.
# ==============================================================================

$taskName = "UnifiedQuantFleet_AutoStart"
$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder "UnifiedQuantFleet.lnk"

Write-Host "Dang go bo cau hinh tu dong khoi dong..." -ForegroundColor Yellow

# 1. Unregister Scheduled Task
try {
    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
    Write-Host "✅ Da xoa Scheduled Task: $taskName" -ForegroundColor Green
} catch {
    Write-Host "ℹ️ Scheduled Task khong ton tai hoac khong the xoa."
}

# 2. Remove Startup Shortcut
if (Test-Path $shortcutPath) {
    Remove-Item $shortcutPath -Force
    Write-Host "✅ Da xoa Startup Shortcut: $shortcutPath" -ForegroundColor Green
} else {
    Write-Host "ℹ️ Startup Shortcut khong ton tai."
}

Write-Host "Hoan tat go bo." -ForegroundColor Cyan
