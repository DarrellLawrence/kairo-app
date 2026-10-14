# Windows settings and one-time logins for Kairo. PowerShell 5.1, ASCII-only.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Kairo.psm1') -Force -DisableNameChecking

$script:TailscaleExe = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'

# ---------------------------------------------------------------- prerequisites

function Assert-Winget {
    if (Get-Command winget.exe -ErrorAction SilentlyContinue) { return }
    Write-Bad 'The Windows "App Installer" (winget) is missing.'
    Write-Note 'Open the Microsoft Store, search "App Installer", click Get/Update, then run Install Kairo again.'
    throw 'winget missing'
}

function Install-WingetPackage([string]$Id, [string]$Name, [string[]]$Extra = @()) {
    Write-Note "Installing $Name (this can take a few minutes)..."
    $wingetArgs = @('install', '--id', $Id, '--exact', '--silent', '--accept-package-agreements',
                    '--accept-source-agreements', '--disable-interactivity') + $Extra
    & winget.exe @wingetArgs | Out-Host
    # 0 = installed; -1978335189 (0x8A15002B) = already installed, no upgrade available
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) { throw "Installing $Name failed (winget $LASTEXITCODE)" }
    Write-Ok "$Name installed"
}

function Install-Prerequisites {
    Write-Step 'Programs Kairo needs'
    # winget only when something is missing: the auto-updater runs as SYSTEM, where winget is not available.
    # Python must be installed for all users: Kairo's own account can't read your user folder.
    if (Find-Python312 -MachineWide) { Write-Ok 'Python 3.12 (for all users) present' }
    else { Assert-Winget; Install-WingetPackage 'Python.Python.3.12' 'Python 3.12' @('--scope', 'machine') }
    if (Test-Path $script:TailscaleExe) { Write-Ok 'Tailscale present' }
    else { Assert-Winget; Install-WingetPackage 'Tailscale.Tailscale' 'Tailscale' }
    $py = Find-Python312 -MachineWide
    if (-not $py) { throw 'Python 3.12 was installed but cannot be found. Restart the PC and run Install Kairo again.' }
    return $py
}

# Claude Code: Anthropic's unmodified Windows binary from its official npm
# package, at the exact version this release was tested with, checked against
# the SHA-512 pinned in versions.json. It lives in C:\Kairo\tools so Kairo's own
# account can run it; your user folder is not involved. It never updates itself
# (DISABLE_UPDATES); a new Kairo release brings the next version.
function Install-ClaudeCode($P, [string]$Version, [string]$Sha512Base64) {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw "Kairo supports 64-bit Intel/AMD PCs only (this is $env:PROCESSOR_ARCHITECTURE)." }
    $dir = Join-Path $P.Tools "claude\$Version"
    $exe = Join-Path $dir 'claude.exe'
    if (Test-Path $exe) {
        $have = [string](& $exe --version)
        if ($have -like "$Version*") { Write-Ok "Claude Code $Version present"; return $exe }
    }
    Write-Note "Downloading Claude Code $Version (about 100 MB)..."
    $tmp = Join-Path $P.Downloads "claude-$Version"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    [void](New-Item -ItemType Directory -Path $tmp)
    $tgz = Join-Path $tmp 'claude.tgz'
    $url = "https://registry.npmjs.org/@anthropic-ai/claude-code-win32-x64/-/claude-code-win32-x64-$Version.tgz"
    $old = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'   # 5.1 progress bar makes downloads very slow
    try { Invoke-WebRequest -Uri $url -OutFile $tgz -UseBasicParsing } finally { $ProgressPreference = $old }
    $expected = ([BitConverter]::ToString([Convert]::FromBase64String($Sha512Base64)) -replace '-', '').ToLower()
    $actual = (Get-FileHash -Algorithm SHA512 $tgz).Hash.ToLower()
    if ($actual -ne $expected) { throw 'The Claude Code download does not match the pinned checksum. Nothing was installed.' }
    & tar.exe -xzf $tgz -C $tmp
    if ($LASTEXITCODE -ne 0) { throw 'Unpacking Claude Code failed.' }
    if (-not (Test-Path $dir)) { [void](New-Item -ItemType Directory -Path $dir) }
    Move-Item -Force (Join-Path $tmp 'package\claude.exe') $exe
    Remove-Item -Recurse -Force $tmp
    Write-Ok ("Claude Code installed: " + [string](& $exe --version))
    return $exe
}

# ---------------------------------------------------------------- power & updates

function Set-AlwaysOnPower {
    Write-Step 'Power: never sleep'
    foreach ($a in @(@('/change', 'standby-timeout-ac', '0'), @('/change', 'hibernate-timeout-ac', '0'), @('/hibernate', 'off'))) {
        & powercfg.exe @a | Out-Null
    }
    Write-Ok 'Sleep and hibernate are off on mains power (the screen may still turn off). Fast Startup is off too.'
    Write-Note 'Also set in the BIOS once: "Restore on AC power loss" = On (see README).'
}

# No surprise restarts: active hours 06:00-23:00, updates install Sundays at
# 04:00 (inside Kairo's quiet window), and no automatic restart while someone
# is logged in. After any restart the boot task brings Kairo back.
function Set-WindowsUpdatePolicy {
    Write-Step 'Windows Update: no surprise restarts'
    $ux = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
    $wu = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
    $au = Join-Path $wu 'AU'
    foreach ($k in @($ux, $wu, $au)) { if (-not (Test-Path $k)) { [void](New-Item -Path $k -Force) } }
    $values = @(
        @($ux, 'ActiveHoursStart', 6), @($ux, 'ActiveHoursEnd', 23), @($ux, 'SmartActiveHoursState', 0),
        @($wu, 'SetActiveHours', 1), @($wu, 'ActiveHoursStart', 6), @($wu, 'ActiveHoursEnd', 23),
        @($au, 'NoAutoUpdate', 0), @($au, 'AUOptions', 4), @($au, 'ScheduledInstallDay', 1),
        @($au, 'ScheduledInstallTime', 4), @($au, 'NoAutoRebootWithLoggedOnUsers', 1)
    )
    foreach ($v in $values) { Set-ItemProperty -Path $v[0] -Name $v[1] -Value $v[2] -Type DWord }
    Write-Ok 'Active hours 06:00-23:00; updates install Sundays 04:00; no restart while you are logged in.'
    $edition = (Get-CimInstance Win32_OperatingSystem).Caption
    if ($edition -match 'Home') {
        Write-Warn "$edition may ignore some of these settings. Kairo still comes back by itself after any restart."
    }
}

function Remove-WindowsUpdatePolicy {
    $wu = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
    foreach ($n in @('SetActiveHours', 'ActiveHoursStart', 'ActiveHoursEnd')) { Remove-ItemProperty -Path $wu -Name $n -ErrorAction SilentlyContinue }
    foreach ($n in @('NoAutoUpdate', 'AUOptions', 'ScheduledInstallDay', 'ScheduledInstallTime', 'NoAutoRebootWithLoggedOnUsers')) {
        Remove-ItemProperty -Path (Join-Path $wu 'AU') -Name $n -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------- BitLocker

# Only with a TPM 2.0 and only with the TPM-only protector (no PIN), so the PC
# still boots by itself after a power cut. The recovery key is shown and saved
# to a USB stick if one is plugged in, and the owner must confirm it was saved
# BEFORE encryption starts. Never written to C: (it would be locked inside).
# Encryption is nice to have, never a reason for setup to fail: every problem
# here is a warning.
function Enable-BitLockerIfPossible([string]$LogFile) {
    Write-Step 'Disk encryption (BitLocker)'
    try { Enable-BitLockerSteps $LogFile }
    catch {
        Write-Warn "Disk encryption was not turned on ($($_.Exception.Message.Trim()))."
        Write-Note 'Kairo works without it. Nothing else was changed.'
    }
}

function Enable-BitLockerSteps([string]$LogFile) {
    $edition = ''
    try { $edition = [string](Get-CimInstance Win32_OperatingSystem).Caption } catch { }
    if ($edition -match 'Home' -or -not (Get-Command Get-BitLockerVolume -ErrorAction SilentlyContinue)) {
        Write-Warn "BitLocker is not part of $(if ($edition) { $edition } else { 'this Windows edition' }). Skipped."
        Write-Note 'If Settings -> Privacy & security has "Device encryption", you can turn that on there instead.'
        return
    }
    $tpm = $null
    try { $tpm = Get-Tpm } catch { }
    if (-not $tpm -or -not $tpm.TpmPresent) {
        Write-Warn 'No TPM found. BitLocker skipped (without a TPM it would need a password at every boot, which breaks auto-start).'
        return
    }
    if (-not $tpm.TpmReady) {
        Write-Warn 'A TPM exists but is not ready. Turn it on in the BIOS (often "PTT" on Intel or "fTPM" on AMD), then run Install Kairo again.'
        return
    }
    $vol = Get-BitLockerVolume -MountPoint $env:SystemDrive
    if ($vol.VolumeStatus -ne 'FullyDecrypted') {
        Write-Ok "BitLocker is already $($vol.VolumeStatus) on $env:SystemDrive ($($vol.ProtectionStatus))."
        $rp = @($vol.KeyProtector | Where-Object { $_.KeyProtectorType -eq 'RecoveryPassword' })
        if ($rp.Count -gt 0) { Write-Note "Recovery key ID: $($rp[0].KeyProtectorId). The key itself is wherever you saved it when BitLocker was turned on." }
        return
    }
    Write-Note 'This PC has a TPM 2.0. Turning on BitLocker encrypts the C: drive, so a stolen disk is unreadable.'
    Write-Note 'Kairo still starts by itself after a power cut (the TPM unlocks the disk, no PIN).'
    if (-not (Read-YesNo 'Turn on BitLocker now?' $true)) { Write-Warn 'BitLocker skipped.'; return }

    $added = Add-BitLockerKeyProtector -MountPoint $env:SystemDrive -RecoveryPasswordProtector -WarningAction SilentlyContinue
    $protector = @($added.KeyProtector | Where-Object { $_.KeyProtectorType -eq 'RecoveryPassword' }) | Select-Object -Last 1
    $key = $protector.RecoveryPassword
    $id = $protector.KeyProtectorId
    $text = "BitLocker recovery key for $env:COMPUTERNAME ($env:SystemDrive)`r`nKey ID: $id`r`nRecovery key: $key`r`nCreated: $(Get-Date -Format 'yyyy-MM-dd HH:mm')`r`n"
    # The key never goes into install.log (fix P1.10): the setup log pauses while it is on screen.
    $paused = $false
    if ($LogFile) { try { Stop-Transcript | Out-Null; $paused = $true } catch { } }
    try {

    $saved = @()
    $usb = @(Get-Volume | Where-Object { $_.DriveType -eq 'Removable' -and $_.DriveLetter })
    foreach ($u in $usb) {
        $path = "$($u.DriveLetter):\BitLocker-Recovery-Key-$env:COMPUTERNAME.txt"
        try { Set-Content -Path $path -Value $text -Encoding ASCII; $saved += $path } catch { }
    }
    Write-Host ''
    Write-Host '    ============ BITLOCKER RECOVERY KEY ============' -ForegroundColor Yellow
    Write-Host "    Key ID:        $id" -ForegroundColor Yellow
    Write-Host "    Recovery key:  $key" -ForegroundColor Yellow
    Write-Host '    =================================================' -ForegroundColor Yellow
    if ($saved.Count -gt 0) { foreach ($s in $saved) { Write-Ok "saved to $s (keep this USB stick somewhere safe, not next to the PC)" } }
    else { Write-Warn 'No USB stick found, so the key is only on this screen.' }
    Write-Note 'Also save it in your password manager or take a photo of it now. Without it, a BIOS reset or'
    Write-Note 'a new motherboard can lock you out of this disk for good.'
    while ($true) {
        $typed = Read-Host '    Type the first 6 digits of the recovery key to confirm you saved it (or SKIP to cancel)'
        if ($typed -eq 'SKIP') {
            Remove-BitLockerKeyProtector -MountPoint $env:SystemDrive -KeyProtectorId $id | Out-Null
            Write-Warn 'BitLocker not turned on. Nothing was encrypted.'
            return
        }
        if ($typed -eq $key.Substring(0, 6)) { break }
        Write-Warn 'That does not match. Look at the key above and try again.'
    }
    } finally {
        if ($paused) { Start-Transcript -Path $LogFile -Append | Out-Null; Write-Note '(the recovery key part is not in this log)' }
    }
    Enable-BitLocker -MountPoint $env:SystemDrive -TpmProtector -UsedSpaceOnly -SkipHardwareTest -EncryptionMethod XtsAes128 -WarningAction SilentlyContinue | Out-Null
    Write-Ok 'BitLocker is on. Encryption runs in the background; the PC stays usable and Kairo keeps running.'
    if ($saved.Count -gt 0) { Write-Ok "Recovery key saved at: $($saved -join ', ') and wherever you wrote it down." }
    else { Write-Ok 'Recovery key: only where you wrote it down or photographed it.' }
}

# ---------------------------------------------------------------- Tailscale

function Get-TailscaleStatus {
    if (-not (Test-Path $script:TailscaleExe)) { return $null }
    try { return (& $script:TailscaleExe status --json | Out-String | ConvertFrom-Json) } catch { return $null }
}

function Get-KairoUrl {
    $s = Get-TailscaleStatus
    if ($s -and $s.Self -and $s.Self.DNSName) { return 'https://' + ([string]$s.Self.DNSName).TrimEnd('.') }
    return $null
}

function Connect-Tailscale([int]$Port) {
    Write-Step 'Login 1 of 5: Tailscale (private network to your iPhone)'
    $s = Get-TailscaleStatus
    if ($s -and $s.BackendState -eq 'Running') {
        Write-Ok "Already signed in as $($s.Self.HostName)"
    } else {
        Write-Note 'A browser window opens. Sign in with the SAME account as the Tailscale app on your iPhone.'
        Write-Note 'If no browser opens, copy the link shown below into a browser.'
        Wait-Enter
    }
    # --unattended: keeps Tailscale connected when nobody is logged in to Windows.
    & $script:TailscaleExe up --unattended | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Bad 'Tailscale sign-in did not finish. Run "Kairo Logins" later to try again.'; return $false }
    Write-Ok 'Tailscale connected, set to stay on without a logged-in user'
    Write-Note 'Publishing Kairo on your tailnet (HTTPS). If Tailscale prints a link to enable HTTPS / Serve,'
    Write-Note 'open it, click Enable, and come back; the command continues by itself.'
    & $script:TailscaleExe serve --bg $Port | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Bad 'tailscale serve failed. Run "Kairo Logins" later to try again.'; return $false }
    $url = Get-KairoUrl
    Write-Ok "Kairo is reachable on your tailnet at $url"
    return $true
}

# ---------------------------------------------------------------- Claude

function Open-ClaudeLogin($P, [string]$ClaudeExe) {
    Write-Note 'A new window opens with Claude Code. In that window:'
    Write-Note '  1. If asked, pick a colour theme (any) and press Enter.'
    Write-Note '  2. Choose "Claude account with subscription". A browser opens: sign in, click Authorize.'
    Write-Note '     (Already signed in but expired? Type  /login  and press Enter.)'
    Write-Note '  3. Back in the window, press Enter when it says "Login successful".'
    Write-Note '  4. Type  /exit  and press Enter. The window closes and setup continues here.'
    Wait-Enter 'Press Enter to open the Claude window'
    # Fix P1.8: a fresh folder only admins can write, for both the settings and the working folder.
    # Started in data\workspace (or with data\claude-config's settings), this admin window would
    # load hooks and tool servers Kairo's own account can write. Only the login file is copied over.
    if (-not (Test-Path $P.Updater)) { Protect-UpdaterFolder $P }
    $login = Join-Path $P.Updater 'claude-login'
    if (Test-Path $login) { Remove-Item -Recurse -Force $login }
    $cfg = Join-Path $login 'config'
    $work = Join-Path $login 'work'
    [void](New-Item -ItemType Directory -Path $cfg)
    [void](New-Item -ItemType Directory -Path $work)
    $old = $env:CLAUDE_CONFIG_DIR
    $env:CLAUDE_CONFIG_DIR = $cfg
    try { Start-Process -FilePath $ClaudeExe -WorkingDirectory $work -Wait }
    finally { $env:CLAUDE_CONFIG_DIR = $old }
    $new = Join-Path $cfg '.credentials.json'
    # Fix F24: data\ is Kairo's account's to change; an admin's write there only inside the data lock.
    if (Test-Path $new) {
        Invoke-DataLocked $P { Copy-Item -Force $new (Join-Path $P.ClaudeConfig '.credentials.json') }
        [void](Wait-KairoUp)   # the next step tests Claude through Kairo
    }
    Remove-Item -Recurse -Force $login -ErrorAction SilentlyContinue
}

# The Claude Code an admin window may start: only from tools\claude (admins-only), never just a
# path from kairo.env (fix P1.8).
function Resolve-ClaudeExe($P, [string]$FromEnv) {
    $tools = (Join-Path $P.Tools 'claude').TrimEnd('\') + '\'
    if ($FromEnv) {
        $full = ''
        try { $full = [IO.Path]::GetFullPath($FromEnv) } catch { }
        if ($full.StartsWith($tools, 'OrdinalIgnoreCase') -and $full.EndsWith('\claude.exe', 'OrdinalIgnoreCase') -and (Test-Path $full)) { return $full }
    }
    $found = Get-ChildItem $tools -Filter 'claude.exe' -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

# Plain-words reason for a failed Claude check.
function Get-ClaudeProblem($R) {
    if (-not $R) { return 'Kairo itself is not answering on this PC, so Claude could not be tested. Run Kairo Check.' }
    switch ([string]$R.brain) {
        'logged_out'    { return 'Claude says this login is missing or expired. Sign in again.' }
        'usage_limited' { return 'The login works, but your Claude usage limit is reached right now. Nothing to fix; it resets by itself.' }
        'unavailable'   { return "Claude Code could not run for Kairo's account ($([string]$R.error))." }
        default         { return "Claude did not answer as expected ($([string]$R.brain): $([string]$R.error))." }
    }
}

# Shows each check on its own line, so it is clear what works and what not.
function Test-ClaudeForKairo($P) {
    $cred = Join-Path $P.ClaudeConfig '.credentials.json'
    if (Test-Path $cred) { Write-Ok "Login file found ($cred)" } else { Write-Bad 'Login file: missing (not signed in yet)'; return $false }
    Write-Note "Test: one tiny message, sent as Kairo's own account (like after a reboot)..."
    $r = Invoke-SelfTest
    if ($r -and $r.ok) { Write-Ok "Test message: Claude answered in $($r.seconds)s. Claude works for Kairo."; return $true }
    if ($r -and [string]$r.brain -eq 'usage_limited') { Write-Warn (Get-ClaudeProblem $r); return $true }
    Write-Bad ('Test message failed: ' + (Get-ClaudeProblem $r))
    return $false
}

function Connect-Claude($P, [string]$ClaudeExe) {
    Write-Step 'Login 2 of 5: Claude (your Max subscription)'
    if (-not $ClaudeExe) { Write-Bad 'Claude Code is not installed in C:\Kairo\tools\claude. Run Update Kairo, then Kairo Logins.'; return $false }
    # The login is stored in C:\Kairo\data\claude-config, where Kairo's own account
    # can use it after every reboot, with nobody logged in.
    [Environment]::SetEnvironmentVariable('CLAUDE_CONFIG_DIR', $P.ClaudeConfig, 'Process')
    [Environment]::SetEnvironmentVariable('DISABLE_UPDATES', '1', 'Process')
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if (Test-ClaudeForKairo $P) { return $true }
        if ($attempt -eq 3 -or -not (Read-YesNo 'Sign in to Claude now?' $true)) { break }
        Open-ClaudeLogin $P $ClaudeExe
    }
    Write-Warn 'Claude is not working for Kairo yet. Start menu -> Kairo -> Kairo Logins tries again.'
    return $false
}

function Invoke-SelfTest {
    try {
        $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/selftest' -Method Post -UseBasicParsing -TimeoutSec 180
        return ($resp.Content | ConvertFrom-Json)
    } catch { return $null }
}

# The full self-test (server/app/selftest.py); Kairo pushes a failure itself. $null: older release or no answer.
function Invoke-ToolsSelfTest {
    try {
        $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/selftest/tools?notify=1' -Method Post -Body '{}' `
            -ContentType 'application/json' -UseBasicParsing -TimeoutSec 900
        return ($resp.Content | ConvertFrom-Json)
    } catch { return $null }
}

# ---------------------------------------------------------------- Google

# After a data lock Kairo starts again: the Google steps talk to it.
function Wait-KairoUp { return [bool](Wait-Healthy 90) }

function Get-GoogleStatus {
    try { return (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/setup/google/status' -UseBasicParsing -TimeoutSec 10).Content | ConvertFrom-Json }
    catch { return $null }
}

function Select-JsonFile([string]$Title = 'Choose the Google client file you downloaded (client_secret_....json)') {
    Add-Type -AssemblyName System.Windows.Forms
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Title = $Title
    $dlg.Filter = 'Google client file (*.json)|*.json'
    $dlg.InitialDirectory = Join-Path $env:USERPROFILE 'Downloads'
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $dlg.FileName }
    return $null
}

function Wait-GoogleKind([string]$Kind, [int]$Seconds = 600) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $st = Get-GoogleStatus
        if ($st -and $st.$Kind) { return [string]$st.$Kind }
        Start-Sleep -Seconds 3
    }
    return $null
}

function Write-GoogleConsoleSteps([string]$Account) {
    Write-Note 'First, a one-time Google Cloud setup (about 5 minutes). Use a browser on this PC,'
    Write-Note "signed in as $Account. Each step is one page:"
    Write-Note '  1. Open  https://console.cloud.google.com/projectcreate'
    Write-Note '     Project name: Kairo  -> Create.  (Top bar: make sure project "Kairo" is selected.)'
    Write-Note '  2. Open  https://console.cloud.google.com/apis/library/gmail.googleapis.com  -> Enable'
    Write-Note '     Open  https://console.cloud.google.com/apis/library/calendar-json.googleapis.com  -> Enable'
    Write-Note '  3. Open  https://console.cloud.google.com/auth/overview  -> Get started'
    Write-Note "     App name: Kairo. Support email: $Account. Audience: External. Contact email: $Account."
    Write-Note '     Agree -> Create.'
    Write-Note '  4. Left menu "Branding": App name Kairo, User support email and Developer contact email:'
    Write-Note "     $Account. Leave logo, home page, privacy policy and domains EMPTY (they are only for"
    Write-Note '     asking Google to verify the app, which a personal app does not need) -> Save.'
    Write-Note '  5. Left menu "Audience" -> Publish app -> Confirm. Status: "In production".'
    Write-Note '     No privacy policy or domain is needed for this. A personal app stays unverified, which is'
    Write-Note '     fine: at sign-in you click Advanced -> Go to Kairo. Published = no logout every 7 days.'
    Write-Note "     (If you keep it in Testing instead: under Test users add $Account. Kairo then reminds"
    Write-Note '     you on the iPhone the day before Google signs it out.)'
    Write-Note '  6. Left menu "Clients" -> Create client -> Application type: Desktop app -> Name: Kairo'
    Write-Note '     -> Create -> Download JSON.'
}

# Optional second client ("Web application") so Google can be signed in again
# from the iPhone, through Kairo's own tailnet address.
function Connect-GooglePhone($P) {
    $url = Get-KairoUrl
    if (-not $url) { Write-Warn 'Tailscale is not connected, so the iPhone sign-in can not be set up now.'; return $false }
    $redirect = "$url/setup/google/callback"
    Write-Note 'Sign in again from the iPhone (only needed while the app is in "Testing"):'
    Write-Note '  1. Open  https://console.cloud.google.com/auth/clients  -> Create client'
    Write-Note '  2. Application type: Web application. Name: Kairo phone'
    Write-Note '  3. Authorized redirect URIs -> Add URI, paste exactly:'
    Write-Host "       $redirect" -ForegroundColor Cyan
    Write-Note '  4. Create -> Download JSON.'
    Wait-Enter 'When the JSON file is downloaded, press Enter to choose it'
    $file = Select-JsonFile 'Choose the "Kairo phone" client file (client_secret_....json)'
    if (-not $file) { Write-Warn 'No file chosen. Skipped.'; return $false }
    try {
        $json = Get-Content -Raw $file | ConvertFrom-Json
        if (-not $json.web -or -not $json.web.client_id -or -not $json.web.client_secret) { throw 'wrong type' }
    } catch {
        Write-Bad 'That is not a Web-application client file. In step 2, pick "Web application".'
        return $false
    }
    $uris = @()
    if ($json.web.PSObject.Properties['redirect_uris']) { $uris = @($json.web.redirect_uris) }
    if ($uris -notcontains $redirect) {
        Write-Bad "The file does not list $redirect as a redirect URI. Fix step 3 and download it again."
        return $false
    }
    Invoke-DataLocked $P { Copy-Item -Force $file (Join-Path $P.Secrets 'google-web-client.json') }
    Write-Ok 'iPhone sign-in ready: Kairo app -> More -> Google -> Sign in to Google again'
    return $true
}

function Connect-Google($P) {
    Write-Step 'Login 4 of 5: Google (Gmail + Calendar)'
    $st = Get-GoogleStatus
    if (-not $st) { Write-Bad 'Kairo is not answering, so Google can''t be set up now. Run "Kairo Logins" later.'; return $false }
    $account = if ($st.expected) { $st.expected } else { 'your Gmail account' }

    if ($st.read -and $st.write -and -not $st.signed_out) {
        Write-Ok "Connected: $($st.read)"
        if ($st.read_may_end) {
            $end = [DateTimeOffset]::FromUnixTimeSeconds([long]$st.read_may_end).LocalDateTime
            Write-Note "Google 'Testing' mode: this sign-in may end on $($end.ToString('ddd d MMM HH:mm'))."
            Write-Note 'Fix for good: console.cloud.google.com/auth/audience -> Publish app, then sign in again once.'
        }
        if (Read-YesNo 'Sign in to Google again now? (needed once after you publish the app)' $false) {
            Invoke-DataLocked $P { foreach ($k in @('read', 'write')) { $f = Join-Path $P.Secrets "google-$k.json"; if (Test-Path $f) { Remove-Item -Force $f } } }
            [void](Wait-KairoUp)
            $st = Get-GoogleStatus
        } else {
            if (-not $st.phone_client -and (Read-YesNo 'Set up signing in again from the iPhone? (only needed in Testing mode)' $false)) {
                [void](Connect-GooglePhone $P)
            }
            return $true
        }
    }

    if (-not $st.client) {
        Write-GoogleConsoleSteps $account
        Wait-Enter 'When the JSON file is downloaded, press Enter to choose it'
        $file = Select-JsonFile
        if (-not $file) { Write-Warn 'No file chosen. Run "Kairo Logins" later to finish Google.'; return $false }
        try {
            $json = Get-Content -Raw $file | ConvertFrom-Json
            if (-not $json.installed -or -not $json.installed.client_id -or -not $json.installed.client_secret) { throw 'wrong type' }
        } catch {
            Write-Bad 'That is not a Desktop-app client file. In step 6, pick "Desktop app" and download its JSON.'
            return $false
        }
        Invoke-DataLocked $P { Copy-Item -Force $file (Join-Path $P.Secrets 'google-client.json') }
        [void](Wait-KairoUp)
        Write-Ok 'Google client saved (only Kairo and admins can read it)'
    }

    $steps = @(@{ kind = 'read'; what = 'read your mail and calendar' }, @{ kind = 'write'; what = 'make drafts and labels, and send ONLY what you approve' })
    $n = 0
    foreach ($s in $steps) {
        $n++
        $now = Get-GoogleStatus
        if ($now -and $now.$($s.kind) -and -not $now.signed_out) { Write-Ok "Sign-in $n of 2 already done"; continue }
        Write-Note "Sign-in $n of 2: lets Kairo $($s.what). A browser opens:"
        Write-Note "  - choose $account"
        Write-Note '  - "Google hasn''t verified this app": click Advanced -> Go to Kairo (unsafe). It is your own app.'
        Write-Note '  - tick every box -> Continue. The page then says "Done".'
        Wait-Enter 'Press Enter to open the browser'
        $kindFile = Join-Path $P.Secrets "google-$($s.kind).json"
        if (Test-Path $kindFile) { Invoke-DataLocked $P { Remove-Item -Force $kindFile }; [void](Wait-KairoUp) }
        Start-Process "http://127.0.0.1:8000/setup/google/start?kind=$($s.kind)"
        $email = Wait-GoogleKind $s.kind
        if (-not $email) { Write-Bad 'No sign-in seen within 10 minutes. Run "Kairo Logins" later to finish Google.'; return $false }
        Write-Ok "Sign-in $n of 2 done: $email"
    }
    $final = Get-GoogleStatus
    if ($final -and -not $final.phone_client -and $final.read_may_end) {
        if (Read-YesNo 'Is the app still in "Testing"? Then set up signing in again from the iPhone now?' $false) { [void](Connect-GooglePhone $P) }
    }
    return $true
}

# ---------------------------------------------------------------- GitHub (updates)

# Old place (until the auto-updater): DPAPI for the installing user only, so
# the updater task (SYSTEM) could not read it. Moved on the next interactive run.
function Get-LegacyTokenPath($P) { return Join-Path $P.Secrets 'github-token.dpapi' }

# The token lives in C:\Kairo\updater (SYSTEM and admins only, not Kairo's own
# account and not the data folder), encrypted with this PC's DPAPI key.
function Get-TokenPath($P) { return Join-Path $P.Updater 'github-token.bin' }

function Save-GitHubToken($P, [string]$Token) {
    Add-Type -AssemblyName System.Security
    if (-not (Test-Path $P.Updater)) { [void](New-Item -ItemType Directory -Path $P.Updater) }
    Protect-UpdaterFolder $P
    $bytes = [Text.Encoding]::UTF8.GetBytes($Token)
    $enc = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
    Set-Content -Path (Get-TokenPath $P) -Value ([Convert]::ToBase64String($enc)) -Encoding ASCII
    $legacy = Get-LegacyTokenPath $P
    if (Test-Path $legacy) { Invoke-DataLocked $P { Remove-Item -Force $legacy } }
}

function Get-GitHubToken($P) {
    $path = Get-TokenPath $P
    if (Test-Path $path) {
        Add-Type -AssemblyName System.Security
        $enc = [Convert]::FromBase64String((Get-Content -Raw -Path $path).Trim())
        $bytes = [Security.Cryptography.ProtectedData]::Unprotect($enc, $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
        return [Text.Encoding]::UTF8.GetString($bytes)
    }
    $legacy = Get-LegacyTokenPath $P
    if (-not (Test-Path $legacy)) { return $null }
    try {
        $secure = Get-Content -Raw -Path $legacy | ConvertTo-SecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    } catch { return $null }   # another Windows account saved it (e.g. read by the SYSTEM updater)
    Save-GitHubToken $P $plain
    return $plain
}

# Hidden input that accepts Ctrl+V, Shift+Insert and right-click paste, shows
# one * per character, and returns the text without spaces or line breaks.
function Read-Secret([string]$Prompt) {
    if ([Console]::IsInputRedirected) { return ([string](Read-Host "    $Prompt")).Trim() }
    Write-Host -NoNewline "    $Prompt "
    $sb = New-Object System.Text.StringBuilder
    while ($true) {
        $k = [Console]::ReadKey($true)
        if ($k.Key -eq [ConsoleKey]::Enter) { break }
        $ctrl = ($k.Modifiers -band [ConsoleModifiers]::Control) -ne 0
        $shift = ($k.Modifiers -band [ConsoleModifiers]::Shift) -ne 0
        if (($ctrl -and $k.Key -eq [ConsoleKey]::V) -or [int]$k.KeyChar -eq 22 -or ($shift -and $k.Key -eq [ConsoleKey]::Insert)) {
            $clip = ''
            try { $clip = [string](Get-Clipboard -Raw) } catch { }
            $clip = $clip -replace '\s', ''
            [void]$sb.Append($clip)
            Write-Host -NoNewline ('*' * $clip.Length)
            continue
        }
        if ($k.Key -eq [ConsoleKey]::Backspace) {
            if ($sb.Length -gt 0) { $sb.Length = $sb.Length - 1; Write-Host -NoNewline "`b `b" }
            continue
        }
        if ([int]$k.KeyChar -gt 32) { [void]$sb.Append($k.KeyChar); Write-Host -NoNewline '*' }
    }
    Write-Host ''
    return $sb.ToString().Trim()
}

function Test-GitHubToken([string]$Token, [string]$Repo) {
    try {
        $h = @{ Authorization = "Bearer $Token"; Accept = 'application/vnd.github+json'; 'User-Agent' = 'kairo-updater' }
        [void](Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo" -Headers $h -UseBasicParsing)
        return $true
    } catch { return $false }
}

function Connect-GitHub($P, [string]$Repo) {
    Write-Step 'Login 3 of 5: GitHub (so "Update Kairo" can download new versions)'
    $existing = Get-GitHubToken $P
    if ($existing -and (Test-GitHubToken $existing $Repo)) { Write-Ok 'Update access already set up'; return $true }
    Write-Note 'Create a read-only key for the kairo repository (about 1 minute), on any device:'
    Write-Note '  1. github.com -> your profile picture (top right) -> Settings'
    Write-Note '  2. Developer settings (bottom left) -> Personal access tokens -> Fine-grained tokens'
    Write-Note '  3. Generate new token. Name: kairo-home updates. Expiration: 1 year'
    Write-Note "  4. Repository access: Only select repositories -> $Repo"
    Write-Note '  5. Permissions -> Repository permissions -> Contents: Read-only (nothing else)'
    Write-Note '  6. Generate token, copy it (starts with github_pat_)'
    Write-Note 'Paste with Ctrl+V or a right-click. You see one * per character.'
    while ($true) {
        $plain = Read-Secret 'Paste the token here, then press Enter (or just Enter to skip):'
        if ($plain.Length -eq 0) { Write-Warn 'Skipped. Automatic updates stay off until this is done.'; return $false }
        $looks = $plain.StartsWith('github_pat_') -or $plain.StartsWith('ghp_')
        $msg = "Received $($plain.Length) characters, starting with $($plain.Substring(0, [Math]::Min(11, $plain.Length)))"
        if ($looks) { Write-Ok "$msg (a fine-grained token is about 93 characters)" } else { Write-Warn "$msg. A GitHub token starts with github_pat_." }
        if (Test-GitHubToken $plain $Repo) {
            Save-GitHubToken $P $plain
            Write-Ok 'GitHub accepted it. Stored encrypted, readable only by Windows itself and admins.'
            return $true
        }
        Write-Warn "GitHub refused that token for $Repo. Check steps 4 and 5 and paste again."
    }
}

Export-ModuleMember -Function *
