@echo off
rem Double-click me. Runs Uninstall-Kairo.ps1 as administrator with the execution-policy
rem bypass for this run only (nothing is changed system-wide).
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\Uninstall-Kairo.ps1" %*
if errorlevel 1 (
  echo.
  echo Something went wrong. The window with the details stays open above, or see C:\Kairo\data\logs.
  pause
)
