@echo off
rem kairo diag [--json] [--strict] [--deep] [--local]
rem Is Kairo's voice stack healthy? One line per check (PASS / WARN / FAIL), exit code 0 when nothing fails.
rem Installed to C:\Kairo\bin (on the machine PATH) by the installer; any Command Prompt or PowerShell can run it.
rem It only reads: no administrator rights are needed and nothing is changed. It asks the running Kairo what is
rem loaded and where it runs; without a running Kairo it judges the files and says so.
setlocal EnableExtensions DisableDelayedExpansion
for %%I in ("%~dp0..") do set "KAIRO_ROOT=%%~fI"
set "KAIRO_PY=%KAIRO_ROOT%\app\current\venv\Scripts\python.exe"
set "KAIRO_RC=2"
if /i not "%~1"=="diag" goto usage
if not exist "%KAIRO_PY%" goto not_installed
set "KAIRO_ARGS="
for /f "tokens=1* delims= " %%A in ("%*") do set "KAIRO_ARGS=%%B"
set "KAIRO_DATA_DIR=%KAIRO_ROOT%\data"
set "PYTHONDONTWRITEBYTECODE=1"
pushd "%KAIRO_ROOT%\app\current\server" || goto not_installed
"%KAIRO_PY%" -m app.diag --env-file "%KAIRO_ROOT%\data\kairo.env" %KAIRO_ARGS%
set "KAIRO_RC=%ERRORLEVEL%"
popd
exit /b %KAIRO_RC%

:usage
echo usage: kairo diag [--json] [--strict] [--deep] [--local]
echo   --json    the report as JSON (what the Voice check page in the app shows)
echo   --strict  a warning also makes the exit code 1
echo   --deep    also check the checksums of the big model files (slow)
echo   --local   don't ask the running Kairo
exit /b 2

:not_installed
echo Kairo is not installed here: "%KAIRO_PY%" is missing.
exit /b 2
