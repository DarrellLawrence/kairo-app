@echo off
rem Double-click me: goes back to the previous Kairo version.
rem Works from any folder, also Downloads and paths with spaces. Asks Windows
rem for administrator rights once (it re-runs this file as admin; arguments
rem are only passed on when it already runs as admin). PowerShell gets the
rem execution-policy bypass for this run only; nothing changes system-wide.
rem Never closes on an error: it says what went wrong and waits for a key.
setlocal EnableExtensions DisableDelayedExpansion
set "KAIRO_HERE=%~dp0"
set "KAIRO_BAT=%~f0"
if not defined KAIRO_ROOT set "KAIRO_ROOT=C:\Kairo"
set "KAIRO_LOG=%KAIRO_ROOT%\install.log"
set "KAIRO_UNBLOCK=%KAIRO_HERE%installer"
set "KAIRO_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "KAIRO_RC=1"
title Kairo rollback

if not exist "%KAIRO_PS%" goto no_powershell
if not exist "%KAIRO_HERE%installer\Rollback-Kairo.ps1" goto incomplete

rem 1. Administrator rights
"%SystemRoot%\System32\fltmc.exe" >nul 2>&1
if not errorlevel 1 goto admin
echo.
echo   Kairo needs administrator rights. Windows asks next: click Yes.
"%KAIRO_PS%" -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath $env:KAIRO_BAT -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
if errorlevel 1 goto no_admin
exit /b 0

:admin
if not exist "%KAIRO_ROOT%\" mkdir "%KAIRO_ROOT%" >nul 2>&1

rem 2. Files downloaded from the internet carry a "blocked" mark. Remove it.
"%KAIRO_PS%" -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath $env:KAIRO_UNBLOCK -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue"

rem 3. Run
"%KAIRO_PS%" -NoProfile -ExecutionPolicy Bypass -File "%KAIRO_HERE%installer\Rollback-Kairo.ps1" -Root "%KAIRO_ROOT%" %*
set "KAIRO_RC=%ERRORLEVEL%"
if "%KAIRO_RC%"=="0" exit /b 0
goto failed

:no_powershell
echo.
echo   Windows PowerShell is missing on this PC, so Kairo cannot run.
goto failed

:incomplete
echo.
echo   Some Kairo files are missing next to this file.
echo   Right-click the downloaded zip, choose "Extract All...", open the
echo   extracted folder and double-click "Rollback Kairo.bat" there.
goto failed

:no_admin
echo.
echo   Windows did not give administrator rights, so nothing was changed.
echo   Double-click the file again and click Yes when Windows asks.
goto wait

:failed
if exist "%KAIRO_ROOT%\" >>"%KAIRO_LOG%" echo %DATE% %TIME% Rollback Kairo.bat: stopped with error code %KAIRO_RC%
echo.
echo   -----------------------------------------------------------------
echo   Kairo rollback did NOT finish (error code %KAIRO_RC%).
echo   The reason is shown above in red.
echo   Log: %KAIRO_LOG%
echo   -----------------------------------------------------------------

:wait
echo   Press any key to close this window.
pause >nul
exit /b %KAIRO_RC%
