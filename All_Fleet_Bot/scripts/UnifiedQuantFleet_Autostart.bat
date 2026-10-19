@echo off
:: =====================================================================
:: UNIFIED QUANT FLEET - AUTOSTART LOADER
:: Placed in Windows Startup Folder to auto-launch on PC boot / logon.
:: =====================================================================
cd /d "C:\Quant_02\All_Fleet_Bot"
start "UNIFIED QUANT FLEET — 5 BOTS ON 1 PC" cmd.exe /c "start_fleet.bat"
exit
