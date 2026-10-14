# "Uninstall Kairo": stop and remove Kairo. Your data is KEPT unless you type DELETE.
# Python, Tailscale and Claude Code stay installed (remove them in Windows
# Settings -> Apps if you want).
param([string]$Root = 'C:\Kairo', [switch]$CI, [switch]$DeleteData)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Kairo.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'KairoSetup.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'KairoSync.psm1') -Force -DisableNameChecking

$passArgs = @('-Root', "`"$Root`"")
if ($CI) { $passArgs += '-CI' }
if ($DeleteData) { $passArgs += '-DeleteData' }
Assert-Admin $PSCommandPath $passArgs

$P = Get-KairoPaths $Root
$exitCode = 0
try {
    if (-not $CI) {
        Write-Host ''
        Write-Host '  This removes Kairo from this PC.' -ForegroundColor Yellow
        if (-not (Read-YesNo 'Continue?' $false)) { Write-Note 'Nothing was changed.'; exit 0 }
        Write-Note "Your data (database, vault, settings, logins) is in $($P.Data)."
        $answer = Read-Host '    Type DELETE to delete it too, or just press Enter to KEEP it'
        $DeleteData = ($answer -ceq 'DELETE')
    }
    Write-Step 'Stopping Kairo'
    Stop-Kairo $P
    Unregister-KairoTask
    Unregister-Sync
    Remove-ServiceAccount $P
    Remove-StartMenu
    if (Test-Path $P.Updater) { & cmd.exe /c rmdir /s /q "$($P.Updater)" | Out-Null }
    Write-Ok 'Kairo stopped; it will no longer start at boot; its Windows account kairo-svc is removed'
    if (-not $CI) {
        $ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
        if (Test-Path $ts) { & $ts serve reset | Out-Null; Write-Ok 'Kairo is no longer published on your tailnet' }
        Remove-WindowsUpdatePolicy
        Write-Ok 'Windows Update settings back to Windows defaults (power settings left as they are)'
    }
    Write-Step 'Removing files'
    foreach ($d in @($P.App, $P.Downloads)) { if (Test-Path $d) { & cmd.exe /c rmdir /s /q "$d" | Out-Null } }
    if ($DeleteData) {
        & cmd.exe /c rmdir /s /q "$($P.Data)" | Out-Null
        Write-Ok 'Data deleted'
    } else {
        Write-Ok "Data kept in $($P.Data) (installing Kairo again picks it up)"
    }
    # The installer folder and the .bat files are in use right now; remove them a moment after this window closes.
    $cleanup = "ping -n 4 127.0.0.1 >nul & rmdir /s /q `"$($P.Installer)`" & del /q `"$Root\*.bat`""
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $cleanup -WindowStyle Hidden
    Write-Ok 'Kairo is uninstalled'
} catch {
    Write-Bad $_.Exception.Message
    $exitCode = 1
}
if (-not $CI -and $exitCode -eq 0) { Wait-Enter 'Press Enter to close this window' }
exit $exitCode
