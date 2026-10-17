# CI: the voice folder built the way the installer builds it on kairo-home (Invoke-ToolPip, the GPU
# add-on with -NoDeps), the models downloaded by app.voice, then the self-test's voice checks and the
# WebRTC packages, all with Kairo's own venv first and the voice folder appended (as on the PC).
# Owner's report 2026-10-17: two releases never stuck on the PC; this path had never run in CI.
param([Parameter(Mandatory)][string]$Repo, [Parameter(Mandatory)][string]$Work)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot '..\Kairo.psm1') -Force -DisableNameChecking
$server = Join-Path $Repo 'server'
$venv = Join-Path $Work 'venv'
$site = Join-Path $Work 'voice-site'
$models = Join-Path $Work 'voice-models'
if (-not (Test-Path $venv)) {
    & python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'venv failed' }
}
$py = Join-Path $venv 'Scripts\python.exe'
Invoke-Checked $py @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r', (Join-Path $server 'requirements-dev.txt')) 'Kairo venv'
$files = Get-ToolSiteFiles $Repo 'voice'
Write-Host "voice folder from: $($files -join ', ') -> voice-$((Get-FilesHash $files).Substring(0, 12).ToLower())"
if (Test-Path $site) { Remove-Item -Recurse -Force $site }
Invoke-ToolPip $py $site (Join-Path $server 'requirements-voice.txt') 'voice packages'
Invoke-ToolPip $py $site (Join-Path $server 'requirements-voice-gpu.txt') 'voice GPU runtime' -NoDeps
$env:KAIRO_VOICE_SITE = $site
Push-Location $server
try {
    Invoke-Checked $py @('-m', 'app.voice', 'download', '--to', $models) 'voice models'
    Invoke-Checked $py @('tools\voice_site_check.py', $site, $models) 'voice folder check'
} finally { Pop-Location }
Write-Host 'VOICE INSTALL CHECKS PASSED' -ForegroundColor Green
