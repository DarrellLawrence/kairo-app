# "Rollback Kairo": go back to the previous installed version (schema first).
param([string]$Root = 'C:\Kairo', [switch]$CI)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Kairo.psm1') -Force -DisableNameChecking

$passArgs = @('-Root', "`"$Root`"")
if ($CI) { $passArgs += '-CI' }
Assert-Admin $PSCommandPath $passArgs

$P = Get-KairoPaths $Root
$exitCode = 0
try {
    $state = Read-State $P
    if (-not $state.previous -or -not (Test-Path (Get-ReleaseDir $P $state.previous))) {
        throw 'There is no previous version to go back to.'
    }
    Write-Step "Going back from $($state.current) to $($state.previous)"
    $currentDir = Get-ReleaseDir $P $state.current
    $python = Join-Path $currentDir 'venv\Scripts\python.exe'
    # The backup is taken inside the data lock, with Kairo stopped (fix F24).
    Restore-Release $P $currentDir $state.previous $null -BackupWith $python
    if (-not (Get-Health)) { throw 'Kairo is not answering after the rollback. See C:\Kairo\data\logs\kairo.log' }
    $was = $state.current
    $state.current = $state.previous
    $state.previous = $was
    Save-State $P $state
    Set-UpdateHold $P $was   # fix P2.13: the auto-updater must not put it straight back
    Write-Ok "Now running $($state.current). Run Rollback again to switch back to $was."
    Write-Note "Automatic updates skip $was and older; the next newer version installs as usual."
} catch {
    Write-Bad $_.Exception.Message
    $exitCode = 1
}
if (-not $CI -and $exitCode -eq 0) { Wait-Enter 'Press Enter to close this window' }
exit $exitCode
