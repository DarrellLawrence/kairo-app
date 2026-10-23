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
$exitCode = 0
$logFile = Join-Path $Root 'install.log'
$logging = $false

# Everything runs inside try/catch: any error is shown in plain words, written
# to C:\Kairo\install.log and turned into exit code 1 ("Install Kairo.bat"
# then keeps the window open).
try {
    Import-Module (Join-Path $PSScriptRoot 'Kairo.psm1') -Force -DisableNameChecking
    Import-Module (Join-Path $PSScriptRoot 'KairoSetup.psm1') -Force -DisableNameChecking
    Import-Module (Join-Path $PSScriptRoot 'KairoSync.psm1') -Force -DisableNameChecking
    $svc = $null

    $passArgs = @('-Root', "`"$Root`"")
    if ($Logins) { $passArgs += '-Logins' }
    if ($Update) { $passArgs += '-Update' }
    if ($CI) { $passArgs += '-CI' }
    if ($Force) { $passArgs += '-Force' }
    if ($ClaudeBin) { $passArgs += @('-ClaudeBin', "`"$ClaudeBin`"") }
    Assert-Admin $PSCommandPath $passArgs

    if (-not (Test-Path -LiteralPath $Root)) { [void](New-Item -ItemType Directory -Path $Root) }
    Hide-RecoveryKeys $logFile
    Start-Transcript -Path $logFile -Append | Out-Null
    $logging = $true

    $P = Get-KairoPaths $Root
    $interactive = -not ($CI -or $Update)
    $sourceApp = Join-Path (Split-Path $PSScriptRoot -Parent) 'app'
    # The owner's Windows account. The auto-updater runs as SYSTEM: it uses the
    # owner remembered from the last interactive install.
    $state0 = Read-State $P
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    if (Test-RunningAsSystem) { $owner = $state0.owner }
    Initialize-Folders $P

    Write-Host ''
    Write-Host '  Kairo setup' -ForegroundColor Cyan
    Write-Host "  Folder: $Root    Windows account: $owner"

    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 17763) { throw "Windows 10 version 1809 or newer is needed (this is build $build)." }

    $loginsOnly = $Logins -and -not (Test-Path (Join-Path $sourceApp 'release.json'))
    if (-not $loginsOnly) {
        $info = Read-ReleaseInfo $sourceApp

        # 1. Kairo's own limited Windows account + folders it may use. The rights on data\ are set
        #    inside the data lock (fix F24); a first install sets them here too (nothing runs yet).
        Write-Step "Kairo's own Windows account and folders"
        $svc = Initialize-ServiceAccount $P
        if ($owner) {
            if (-not $state0.current) { Set-KairoAcl $P $owner $svc.Name }
            if (-not (Test-RunningAsSystem)) { $state0.owner = $owner; Save-State $P $state0 }
        }
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

        # 3. Settings file. Config: release defaults + data\config (owner changes only);
        #    KAIRO_CONFIG_DIR is for releases from before that (see Save-LegacyConfig).
        #    Written inside the data lock with the rights and the sync settings (step 5).
        $envValues = @{
            KAIRO_DATA_DIR    = $P.Data
            KAIRO_STATUS_DIR  = $P.Status
            KAIRO_CONFIG_DIR  = $P.ConfigLegacy
            KAIRO_CONFIG_OVERRIDES = $P.Config
            KAIRO_CLAUDE_BIN  = $claudeExe
            CLAUDE_CONFIG_DIR = $P.ClaudeConfig
            KAIRO_PORT        = '8000'
        }
        # Vault sync: nice to have, never a reason for the install to fail.
        $sync = @{ ready = $false }
        try { [void](Install-Syncthing $P); $sync.ready = $true } catch { Write-Warn "Vault sync not set up: $($_.Exception.Message.Trim())" }
        # Everything the installer itself writes into data\, run inside the data lock (fix F24).
        $inLock = {
            if ($owner) { Set-KairoAcl $P $owner $svc.Name }
            Save-LegacyConfig $P
            Write-EnvFile $P $envValues
            if ($sync.ready) {
                try { Initialize-SyncConfig $P }
                catch { $sync.ready = $false; Write-Warn "Vault sync not set up: $($_.Exception.Message.Trim())" }
            }
        }

        # 4. Windows settings (real PC only)
        if (-not $CI) {
            Set-AlwaysOnPower
            Set-WindowsUpdatePolicy
            if ($interactive) { Enable-BitLockerIfPossible $logFile }
        }

        # 5. Kairo itself (backup, switch, migrate, health check, auto-rollback), running as kairo-svc
        $ok = Deploy-Release $P $sourceApp $python $svc -Force:$Force -InLock $inLock
        if (-not $ok) { $exitCode = 1 }

        # 6. Newest installer, boot check, and the double-click files in C:\Kairo
        Copy-Tree $PSScriptRoot $P.Installer
        foreach ($bat in @('Update Kairo.bat', 'Rollback Kairo.bat', 'Uninstall Kairo.bat', 'Kairo Logins.bat', 'Kairo Check.bat')) {
            Copy-Item -Force (Join-Path $PSScriptRoot "bat\$bat") (Join-Path $Root $bat)
        }
        Install-Launcher $P (Join-Path $PSScriptRoot 'bat\kairo.cmd')
        # The .bat files only work from C:\Kairo; copies in installer\bat only confused.
        $batCopies = Join-Path $P.Installer 'bat'
        if (Test-Path $batCopies) { Remove-Item -Recurse -Force $batCopies }
        Install-StartMenu $P
        Register-BootCheckTask $P
        Register-AutoUpdateTask $P
        if ($sync.ready) {
            try { Start-SyncTask $P $svc } catch { Write-Warn "Vault sync not set up: $($_.Exception.Message.Trim())" }
        }
    }

    # 7. One-time logins
    if (($interactive -or $Logins) -and -not $CI -and $exitCode -eq 0) {
        $repo = $null
        if (Test-Path (Join-Path $P.Current 'release.json')) { $repo = [string](Read-ReleaseInfo $P.Current).update_repo }
        $results = [ordered]@{}
        $results['Tailscale'] = Connect-Tailscale 8000
        $results['Claude'] = Connect-Claude $P (Resolve-ClaudeExe $P ((Read-EnvFile $P)['KAIRO_CLAUDE_BIN']))
        if ($repo) { $results['GitHub updates'] = Connect-GitHub $P $repo }
        $results['Google'] = Connect-Google $P
        $results['Vault sync'] = Connect-Sync $P $svc

        Write-Step 'Summary'
        foreach ($k in $results.Keys) {
            if ($results[$k]) { Write-Ok $k } else { Write-Warn "$k not done. Start menu -> Kairo -> Kairo Logins finishes it." }
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
    Write-Host ''
    Write-Host "    XX  $($_.Exception.Message)" -ForegroundColor Red
    if ($_.InvocationInfo -and $_.InvocationInfo.ScriptName) {
        Write-Host "        (in $(Split-Path $_.InvocationInfo.ScriptName -Leaf), line $($_.InvocationInfo.ScriptLineNumber))"
    }
    if (-not $logging) {
        try {
            if (-not (Test-Path -LiteralPath $Root)) { [void](New-Item -ItemType Directory -Path $Root) }
            Add-Content -LiteralPath $logFile -Value ("{0} Install-Kairo.ps1: {1}`r`n{2}" -f (Get-Date -Format 's'), $_.Exception.Message, $_.ScriptStackTrace)
        } catch { }
    }
    Write-Host "    Log: $logFile"
    $exitCode = 1
} finally {
    if ($logging) {
        Stop-Transcript | Out-Null
        # The log can hold what was typed during setup: owner and admins only,
        # not Kairo's own account.
        try {
            $grants = @('*S-1-5-18:F', '*S-1-5-32-544:F')
            if ($owner -and -not (Test-RunningAsSystem)) { $grants += "${owner}:R" }
            & icacls.exe $logFile /inheritance:r /grant:r @grants /Q | Out-Null
        } catch { }
    }
}

# Success: wait so the owner can read the address. Failure: the .bat waits.
if ($exitCode -eq 0 -and -not $CI -and -not $Update) { [void](Read-Host '    Press Enter to close this window') }
exit $exitCode
