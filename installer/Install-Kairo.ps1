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

        # 1. Kairo's own limited Windows account + folders it may use
        Write-Step "Kairo's own Windows account and folders"
        $svc = Initialize-ServiceAccount
        Set-KairoAcl $P $owner $svc.Name
        Write-Ok "account kairo-svc ready (no admin rights, hidden from the sign-in screen)"
        Write-Ok "$Root ready (code: read-only for Kairo; data: Kairo and you; nobody else)"

        # 2. Programs
        if ($CI) {
            $python = Find-Python312 -MachineWide
            if (-not $python) { throw 'CI: set KAIRO_PYTHON to a Python 3.12 executable' }
            $claudeExe = $ClaudeBin
        } else {
            $python = Install-Prerequisites
            $versions = Get-Content -Raw (Join-Path $PSScriptRoot 'versions.json') | ConvertFrom-Json
            $claudeExe = if ($ClaudeBin) { $ClaudeBin } else {
                Install-ClaudeCode $P ([string]$info.claude_code_version) ([string]$versions.claude_code_win_x64_sha512)
            }
        }

        # 3. Settings file
        Write-EnvFile $P @{
            KAIRO_DATA_DIR    = $P.Data
            KAIRO_CONFIG_DIR  = $P.Config
            KAIRO_CLAUDE_BIN  = $claudeExe
            CLAUDE_CONFIG_DIR = $P.ClaudeConfig
            KAIRO_PORT        = '8000'
        }

        # 4. Windows settings (real PC only)
        if (-not $CI) {
            Set-AlwaysOnPower
            Set-WindowsUpdatePolicy
            if ($interactive) { Enable-BitLockerIfPossible }
        }

        # 5. Kairo itself (backup, switch, migrate, health check, auto-rollback), running as kairo-svc
        $ok = Deploy-Release $P $sourceApp $python $svc -Force:$Force
        if (-not $ok) { $exitCode = 1 }

        # 6. Newest installer, boot check, and the double-click files in C:\Kairo
        Copy-Tree $PSScriptRoot $P.Installer
        foreach ($bat in @('Update Kairo.bat', 'Rollback Kairo.bat', 'Uninstall Kairo.bat', 'Kairo Logins.bat', 'Kairo Check.bat')) {
            Copy-Item -Force (Join-Path $PSScriptRoot "bat\$bat") (Join-Path $Root $bat)
        }
        Register-BootCheckTask $P
    }

    # 7. One-time logins
    if (($interactive -or $Logins) -and -not $CI -and $exitCode -eq 0) {
        $repo = $null
        if (Test-Path (Join-Path $P.Current 'release.json')) { $repo = [string](Read-ReleaseInfo $P.Current).update_repo }
        $results = [ordered]@{}
        $results['Tailscale'] = Connect-Tailscale 8000
        $results['Claude'] = Connect-Claude $P ((Read-EnvFile $P)['KAIRO_CLAUDE_BIN'])
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
            Write-Note 'Last check: restart this PC and do NOT log in. After about 5 minutes, on the iPhone:'
            Write-Note '  open Kairo -> More: "Last boot check" should be all green. Then send a chat message.'
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
