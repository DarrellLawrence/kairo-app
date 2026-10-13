@echo off
rem Double-click me. Runs Kairo-BootCheck.ps1 as administrator with the execution-policy
rem bypass for this run only (nothing is changed system-wide).
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\Kairo-BootCheck.ps1" -Interactive %*
if errorlevel 1 (
  echo.
  echo Something is not right. The window with the details stays open above, or see C:\Kairo\data\logs.
  pause
)
