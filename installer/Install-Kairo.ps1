# Install or update Kairo on this Windows PC. Started by "Install Kairo.bat".
# Safe to run again: everything already done is skipped.
#
#   -Logins   only the one-time logins (Tailscale, Claude, GitHub)
#   -Update   called by "Update Kairo": no questions, no logins
#   -CI       automated test on a build server: no downloads, no logins,
#             no changes to power, Windows Update or BitLocker
param(
    [string]$Root = 'C:\Kairo',
    [switch]$Logins,
    [switch]$Update,
    [switch]$CI,
    [switch]$Force,
    [string]$ClaudeBin = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Kairo.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'KairoSetup.psm1') -Force -DisableNameChecking

$passArgs = @('-Root', "`"$Root`"")
if ($Logins) { $passArgs += '-Logins' }
if ($Update) { $passArgs += '-Update' }
if ($CI) { $passArgs += '-CI' }
if ($Force) { $passArgs += '-Force' }
if ($ClaudeBin) { $passArgs += @('-ClaudeBin', "`"$ClaudeBin`"") }
Assert-Admin $PSCommandPath $passArgs

$P = Get-KairoPaths $Root
$interactive = -not ($CI -or $Update)
$sourceApp = Join-Path (Split-Path $PSScriptRoot -Parent) 'app'
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$exitCode = 0

Initialize-Folders $P
$logFile = Join-Path $P.Logs ("install-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
Start-Transcript -Path $logFile -Append | Out-Null

try {
    Write-Host ''
    Write-Host '  Kairo setup' -ForegroundColor Cyan
    Write-Host "  Folder: $Root    Windows account: $owner"

    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 17763) { throw "Windows 10 version 1809 or newer is needed (this is build $build)." }

    $loginsOnly = $Logins -and -not (Test-Path (Join-Path $sourceApp 'release.json'))
    if (-not $loginsOnly) {
        $info = Read-ReleaseInfo $sourceApp

        # 1. Programs
        if ($CI) {
            $python = Find-Python312
            if (-not $python) { throw 'CI: set KAIRO_PYTHON to a Python 3.12 executable' }
            $claudeExe = $ClaudeBin
        } else {
            $python = Install-Prerequisites ([string]$info.claude_code_version)
            $claudeExe = if ($ClaudeBin) { $ClaudeBin } else { Get-ClaudeExe }
        }

        # 2. Folders, permissions, settings file
        Write-Step 'Folders and permissions'
        Set-KairoAcl $P $owner
        Write-EnvFile $P @{
            KAIRO_DATA_DIR    = $P.Data
            KAIRO_CONFIG_DIR  = $P.Config
            KAIRO_CLAUDE_BIN  = $claudeExe
            CLAUDE_CONFIG_DIR = $P.ClaudeConfig
            KAIRO_PORT        = '8000'
        }
        Write-Ok "$Root ready (code: admins only; data: you)"

        # 3. Windows settings (real PC only)
        if (-not $CI) {
            Set-AlwaysOnPower
            Set-WindowsUpdatePolicy
            if ($interactive) { Enable-BitLockerIfPossible }
        }

        # 4. Kairo itself (backup, switch, migrate, health check, auto-rollback)
        $ok = Deploy-Release $P $sourceApp $python $owner -Force:$Force
        if (-not $ok) { $exitCode = 1 }

        # 5. Keep the newest installer + the double-click files in C:\Kairo
        Copy-Tree $PSScriptRoot $P.Installer
        foreach ($bat in @('Update Kairo.bat', 'Rollback Kairo.bat', 'Uninstall Kairo.bat', 'Kairo Logins.bat')) {
            Copy-Item -Force (Join-Path $PSScriptRoot "bat\$bat") (Join-Path $Root $bat)
        }
    }

    # 6. One-time logins
    if (($interactive -or $Logins) -and -not $CI -and $exitCode -eq 0) {
        $repo = $null
        if (Test-Path (Join-Path $P.Current 'release.json')) { $repo = [string](Read-ReleaseInfo $P.Current).update_repo }
        $results = [ordered]@{}
        $results['Tailscale'] = Connect-Tailscale 8000
        $claudeForLogin = (Read-EnvFile $P)['KAIRO_CLAUDE_BIN']
        if (-not $claudeForLogin) { $claudeForLogin = Get-ClaudeExe }
        $results['Claude'] = Connect-Claude $P $claudeForLogin
        if ($repo) { $results['GitHub updates'] = Connect-GitHub $P $repo }
        Write-Step 'Google (Gmail + Calendar)'
        Write-Note 'Comes with the next Kairo version (Milestone B). "Update Kairo" will add it.'

        Write-Step 'Summary'
        foreach ($k in $results.Keys) {
            if ($results[$k]) { Write-Ok $k } else { Write-Warn "$k not done. Double-click C:\Kairo\Kairo Logins.bat to finish it." }
        }
    }

    if ($exitCode -eq 0 -and -not $CI) {
        $url = Get-KairoUrl
        Write-Step 'Done'
        if ($url) {
            Write-Note "On your iPhone (Tailscale app connected): open Safari -> $url"
            Write-Note 'Then Share -> Add to Home Screen -> Add.'
        }
        if ($interactive) {
            Write-Note 'Last check: restart this PC once and do NOT log in. After ~3 minutes Kairo should open on the iPhone.'
        }
    }
} catch {
    Write-Bad $_.Exception.Message
    Write-Note "Full log: $logFile"
    $exitCode = 1
} finally {
    Stop-Transcript | Out-Null
}

if (-not $CI -and -not $Update) { Wait-Enter 'Press Enter to close this window' }
exit $exitCode
