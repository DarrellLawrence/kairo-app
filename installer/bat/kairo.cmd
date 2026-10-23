@echo off
rem kairo diag [--json] [--strict] [--deep] [--local]   |   kairo bench [--clips N] [--strict] ...
rem Is Kairo's voice stack healthy? One line per check (PASS / WARN / FAIL), exit code 0 when nothing fails.
rem Installed to C:\Kairo\bin (on the machine PATH) by the installer; any Command Prompt or PowerShell can run it.
rem It only reads: no administrator rights are needed and nothing is changed. It asks the running Kairo what is
rem loaded and where it runs; without a running Kairo it judges the files and says so.
setlocal EnableExtensions DisableDelayedExpansion
for %%I in ("%~dp0..") do set "KAIRO_ROOT=%%~fI"
set "KAIRO_PY=%KAIRO_ROOT%\app\current\venv\Scripts\python.exe"
set "KAIRO_RC=2"
set "KAIRO_MOD="
if /i "%~1"=="diag" set "KAIRO_MOD=app.diag"
if /i "%~1"=="bench" set "KAIRO_MOD=app.bench_voice"
if not defined KAIRO_MOD goto usage
if not exist "%KAIRO_PY%" goto not_installed
set "KAIRO_ARGS="
for /f "tokens=1* delims= " %%A in ("%*") do set "KAIRO_ARGS=%%B"
set "KAIRO_DATA_DIR=%KAIRO_ROOT%\data"
set "PYTHONDONTWRITEBYTECODE=1"
pushd "%KAIRO_ROOT%\app\current\server" || goto not_installed
"%KAIRO_PY%" -m %KAIRO_MOD% --env-file "%KAIRO_ROOT%\data\kairo.env" %KAIRO_ARGS%
set "KAIRO_RC=%ERRORLEVEL%"
popd
exit /b %KAIRO_RC%

:usage
echo usage: kairo diag [--json] [--strict] [--deep] [--local]
echo   --json    the report as JSON (what the Voice check page in the app shows)
echo   --strict  a warning also makes the exit code 1
echo   --deep    also check the checksums of the big model files (slow)
echo   --local   don't ask the running Kairo
echo.
echo usage: kairo bench [--clips N] [--warmup N] [--strict] [--force] [--mock] [--url URL]
echo   Times calls the way you hear them: a fake phone on this PC calls Kairo with recorded lines.
echo   --clips   only N of the lines (default: all, about 3 minutes)
echo   --warmup  lines played first and not counted (default 1)
echo   --strict  exit code 1 when the run misses its targets
echo   --force   call even if a call seems to be on (it gets replaced)
echo   --mock    the same test against fake models of known speed (checks the test itself)
echo   --url     Kairo's address (default this PC)
exit /b 2

:not_installed
echo Kairo is not installed here: "%KAIRO_PY%" is missing.
exit /b 2
