# Shared functions for the Kairo installer, updater, rollback and uninstaller.
# Windows PowerShell 5.1 compatible. Keep this file ASCII-only.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:TaskName = 'Kairo'
$script:BootCheckTaskName = 'Kairo Boot Check'
$script:AutoUpdateTaskName = 'Kairo Auto-Update'
$script:StartMenu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\Kairo'
$script:TaskPath = '\Kairo\'
$script:Port = 8000
$script:ServiceUser = 'kairo-svc'   # Kairo's own limited Windows account

# ---------------------------------------------------------------- output

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "        $Text" }
function Write-Warn([string]$Text) { Write-Host "    !!  $Text" -ForegroundColor Yellow }
function Write-Bad([string]$Text) { Write-Host "    XX  $Text" -ForegroundColor Red }

function Read-YesNo([string]$Question, [bool]$Default = $true) {
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    while ($true) {
        $answer = Read-Host "    $Question $hint"
        if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
        switch -Regex ($answer.Trim()) {
            '^(y|yes|j|ja)$' { return $true }
            '^(n|no|nein)$' { return $false }
        }
    }
}

function Wait-Enter([string]$Text = 'Press Enter to continue') { [void](Read-Host "    $Text") }

# ---------------------------------------------------------------- admin

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Re-launches the calling script elevated (one UAC prompt) and exits this copy.
function Assert-Admin([string]$ScriptPath, [string[]]$ScriptArgs) {
    if (Test-Admin) { return }
    Write-Host 'Kairo needs administrator rights. Windows will ask: click Yes.' -ForegroundColor Cyan
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$ScriptPath`"") + $ScriptArgs
    try {
        $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -PassThru
        $null = $proc.Handle   # without the cached handle, ExitCode can come back empty (= success)
        $proc.WaitForExit()
        if ($null -eq $proc.ExitCode) { exit 1 }
        exit $proc.ExitCode
    } catch {
        Write-Bad 'Administrator rights were not granted. Nothing was changed.'
        exit 1
    }
}

# ---------------------------------------------------------------- paths & state

function Get-KairoPaths([string]$Root) {
    $data = Join-Path $Root 'data'
    $app = Join-Path $Root 'app'
    return [pscustomobject]@{
        Root         = $Root
        App          = $app
        Releases     = Join-Path $app 'releases'
        Current      = Join-Path $app 'current'
        State        = Join-Path $app 'state.json'
        Installer    = Join-Path $Root 'installer'
        Updater      = Join-Path $Root 'updater'
        Vault        = Join-Path $data 'vault'
        Tools        = Join-Path $Root 'tools'
        Downloads    = Join-Path $Root 'downloads'
        Data         = $data
        Config       = Join-Path $data 'config'          # only what the owner changed (since 2026-10-14)
        ConfigLegacy = Join-Path $data 'config-legacy'   # full copy for releases from before that (rollback)
        Secrets      = Join-Path $data 'secrets'
        Logs         = Join-Path $data 'logs'
        Backups      = Join-Path $data 'backups'
        Workspace    = Join-Path $data 'workspace'
        ClaudeConfig = Join-Path $data 'claude-config'
        Db           = Join-Path (Join-Path $data 'db') 'kairo.sqlite'
        EnvFile      = Join-Path $data 'kairo.env'
    }
}

function Read-State($P) {
    if (Test-Path $P.State) {
        $s = Get-Content -Raw -Path $P.State | ConvertFrom-Json
        $owner = if ($s.PSObject.Properties['owner']) { $s.owner } else { $null }
        return [pscustomobject]@{ current = $s.current; previous = $s.previous; history = @($s.history | Where-Object { $_ }); owner = $owner }
    }
    return [pscustomobject]@{ current = $null; previous = $null; history = @(); owner = $null }
}

function Save-State($P, $State) {
    $State | ConvertTo-Json -Depth 4 | Set-Content -Path $P.State -Encoding UTF8
}

function Read-ReleaseInfo([string]$AppDir) {
    return Get-Content -Raw -Path (Join-Path $AppDir 'release.json') | ConvertFrom-Json
}

# ---------------------------------------------------------------- folders & permissions

function Initialize-Folders($P) {
    foreach ($d in @($P.Root, $P.App, $P.Releases, $P.Installer, $P.Tools, $P.Downloads, $P.Data, $P.Vault, $P.Config, $P.Secrets,
                     $P.Logs, $P.Backups, $P.Workspace, $P.ClaudeConfig, (Split-Path $P.Db))) {
        if (-not (Test-Path $d)) { [void](New-Item -ItemType Directory -Path $d) }
    }
}

# Code and tools: read/run for Kairo's account and the owner; only admins can
# change them (nothing Kairo runs, the model included, can rewrite its own
# code). Data: Kairo's account and the owner can change it. Everyone else:
# nothing.
# C:\Kairo\updater: the update token and the updater's state. Windows itself
# (SYSTEM) and admins only; not the owner's normal rights, not Kairo's account.
function Protect-UpdaterFolder($P) {
    if (-not (Test-Path $P.Updater)) { [void](New-Item -ItemType Directory -Path $P.Updater) }
    & icacls.exe $P.Updater /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed on $($P.Updater)" }
}

function Set-KairoAcl($P, [string]$Owner, [string]$Service) {
    $sys = '*S-1-5-18'; $admins = '*S-1-5-32-544'
    & icacls.exe $P.Root /inheritance:r /grant:r "${sys}:(OI)(CI)F" "${admins}:(OI)(CI)F" "${Owner}:(OI)(CI)RX" "${Service}:(OI)(CI)RX" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed on $($P.Root)" }
    & icacls.exe $P.Data /grant:r "${Owner}:(OI)(CI)M" "${Service}:(OI)(CI)M" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed on $($P.Data)" }
    Protect-UpdaterFolder $P
}

function Test-RunningAsSystem { return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq 'S-1-5-18' }

# ---------------------------------------------------------------- Kairo's own Windows account

function New-RandomPassword {
    $bytes = New-Object byte[] 30
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes); $rng.Dispose()
    return [Convert]::ToBase64String($bytes) + 'aA1!'   # + one of each class for password policies
}

# kairo-svc: a standard (non-admin) local account that only Kairo uses. It
# gets a new random password on every install, which only Task Scheduler
# keeps (Kairo never stores it), is hidden from the sign-in screen, and can
# only reach C:\Kairo plus what every Windows user can (not your files).
function Initialize-ServiceAccount {
    $name = $script:ServiceUser
    $plain = New-RandomPassword
    $secure = ConvertTo-SecureString $plain -AsPlainText -Force
    $user = Get-LocalUser -Name $name -ErrorAction SilentlyContinue
    if ($user) {
        Set-LocalUser -Name $name -Password $secure -PasswordNeverExpires $true -AccountNeverExpires
        Enable-LocalUser -Name $name
    } else {
        [void](New-LocalUser -Name $name -Password $secure -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires `
            -FullName 'Kairo' -Description 'Runs Kairo in the background. No sign-in.')
    }
    $usersGroup = (Get-LocalGroup -SID 'S-1-5-32-545').Name
    $adminsGroup = (Get-LocalGroup -SID 'S-1-5-32-544').Name
    if (-not (Get-LocalGroupMember -Group $usersGroup -Member $name -ErrorAction SilentlyContinue)) { Add-LocalGroupMember -Group $usersGroup -Member $name }
    if (Get-LocalGroupMember -Group $adminsGroup -Member $name -ErrorAction SilentlyContinue) { Remove-LocalGroupMember -Group $adminsGroup -Member $name }
    $hide = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList'
    if (-not (Test-Path $hide)) { [void](New-Item -Path $hide -Force) }
    Set-ItemProperty -Path $hide -Name $name -Value 0 -Type DWord
    $full = "$env:COMPUTERNAME\$name"
    # May run as a background task at boot; may NOT sign in at the PC or over Remote Desktop.
    Grant-UserRight $full 'SeBatchLogonRight'
    Grant-UserRight $full 'SeDenyInteractiveLogonRight'
    Grant-UserRight $full 'SeDenyRemoteInteractiveLogonRight'
    return [pscustomobject]@{ Name = $full; Password = $plain }
}

# User rights via the LSA API (no secedit export/import round trip).
$script:LsaCode = @'
using System;
using System.Runtime.InteropServices;
using System.Security.Principal;
public static class KairoLsa {
    [StructLayout(LayoutKind.Sequential)] struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)] struct LSA_OBJECT_ATTRIBUTES { public int Length; public IntPtr RootDirectory; public IntPtr ObjectName; public uint Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }
    [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr systemName, ref LSA_OBJECT_ATTRIBUTES attrs, uint access, out IntPtr handle);
    [DllImport("advapi32.dll")] static extern uint LsaAddAccountRights(IntPtr handle, byte[] sid, LSA_UNICODE_STRING[] rights, uint count);
    [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr handle);
    [DllImport("advapi32.dll")] static extern int LsaNtStatusToWinError(uint status);
    public static void Grant(string account, string right) {
        var sid = (SecurityIdentifier)new NTAccount(account).Translate(typeof(SecurityIdentifier));
        var bytes = new byte[sid.BinaryLength];
        sid.GetBinaryForm(bytes, 0);
        var attrs = new LSA_OBJECT_ATTRIBUTES();
        IntPtr handle;
        uint r = LsaOpenPolicy(IntPtr.Zero, ref attrs, 0x000F0FFF, out handle);
        if (r != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(r));
        try {
            var s = new LSA_UNICODE_STRING();
            s.Buffer = Marshal.StringToHGlobalUni(right);
            s.Length = (ushort)(right.Length * 2);
            s.MaximumLength = (ushort)((right.Length + 1) * 2);
            try {
                r = LsaAddAccountRights(handle, bytes, new[] { s }, 1);
                if (r != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(r));
            } finally { Marshal.FreeHGlobal(s.Buffer); }
        } finally { LsaClose(handle); }
    }
}
'@

function Grant-UserRight([string]$Account, [string]$Right) {
    if (-not ('KairoLsa' -as [type])) { Add-Type -TypeDefinition $script:LsaCode }
    [KairoLsa]::Grant($Account, $Right)
}

function Remove-ServiceAccount {
    $name = $script:ServiceUser
    $user = Get-LocalUser -Name $name -ErrorAction SilentlyContinue
    if (-not $user) { return }
    $sid = $user.SID.Value
    Remove-LocalUser -Name $name
    Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $sid } | Remove-CimInstance -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList' -Name $name -ErrorAction SilentlyContinue
}

function Copy-Tree([string]$From, [string]$To) {
    if (-not (Test-Path $To)) { [void](New-Item -ItemType Directory -Path $To) }
    & robocopy.exe $From $To /E /NFL /NDL /NJH /NJS /NP /R:2 /W:1 | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $From to $To failed (robocopy $LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}

# Config in layers (2026-10-14): the defaults come with every release, data\config holds only
# what the owner changed, and Kairo builds data\config-effective at every start (it turns the
# old full copies into "only your changes" once, with a backup). Releases from before that read
# KAIRO_CONFIG_DIR; it points to a frozen full copy, so a rollback to one of them still starts.
function Save-LegacyConfig($P) {
    $migrated = Test-Path (Join-Path $P.Config '.overrides-v1')
    $full = Test-Path (Join-Path $P.Config 'permissions.yaml')
    if ($full -and -not $migrated -and -not (Test-Path $P.ConfigLegacy)) {
        Copy-Tree $P.Config $P.ConfigLegacy
        Write-Ok 'config: full copy kept in data\config-legacy for older versions'
    }
}

# kairo.env: Kairo-managed keys are rewritten, anything else in the file is kept.
function Write-EnvFile($P, [hashtable]$Values) {
    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path $P.EnvFile) {
        foreach ($line in Get-Content -Path $P.EnvFile) {
            $key = ($line -split '=', 2)[0].Trim()
            if (-not $Values.ContainsKey($key)) { $lines.Add($line) }
        }
    } else {
        $lines.Add('# Written by the Kairo installer. Lines you add here are kept on updates.')
    }
    foreach ($k in ($Values.Keys | Sort-Object)) { $lines.Add("$k=$($Values[$k])") }
    Set-Content -Path $P.EnvFile -Value $lines -Encoding UTF8
}

function Read-EnvFile($P) {
    $h = @{}
    if (Test-Path $P.EnvFile) {
        foreach ($line in Get-Content -Path $P.EnvFile) {
            if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
            $kv = $line -split '=', 2
            $h[$kv[0].Trim()] = $kv[1].Trim()
        }
    }
    return $h
}

function Use-KairoEnv($P) {
    $envs = Read-EnvFile $P
    foreach ($k in $envs.Keys) { [Environment]::SetEnvironmentVariable($k, $envs[$k], 'Process') }
}

# ---------------------------------------------------------------- python

# -MachineWide: ignore a Python inside a user profile (Kairo's account can't read it).
function Find-Python312([switch]$MachineWide) {
    $profiles = Join-Path $env:SystemDrive 'Users'
    if ($env:KAIRO_PYTHON -and (Test-Path $env:KAIRO_PYTHON)) { return $env:KAIRO_PYTHON }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        # No stderr redirect here: in PowerShell 5.1 with ErrorAction Stop it would throw.
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { $exe = & $py.Source -3.12 -c 'import sys; print(sys.executable)' } catch { $exe = $null }
        finally { $ErrorActionPreference = $old }
        if ($LASTEXITCODE -eq 0 -and $exe) {
            $found = ([string]$exe).Trim()
            if (-not ($MachineWide -and $found.StartsWith($profiles, 'OrdinalIgnoreCase'))) { return $found }
        }
    }
    $candidates = @("$env:ProgramFiles\Python312\python.exe")
    if (-not $MachineWide) { $candidates += "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" }
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments, [string]$What) {
    # Out-Host: a native command's output would otherwise become this function's return value.
    & $Exe @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

# ---------------------------------------------------------------- releases

function Get-ReleaseDir($P, [string]$Version) { return Join-Path $P.Releases $Version }

function Install-ReleaseFiles($P, [string]$SourceApp, [string]$Version, [string]$Python) {
    $dest = Get-ReleaseDir $P $Version
    $venvPy = Join-Path $dest 'venv\Scripts\python.exe'
    if ((Test-Path $venvPy) -and (Test-Path (Join-Path $dest '.complete'))) {
        Write-Ok "release $Version already installed"
        return $dest
    }
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Copy-Tree $SourceApp $dest
    Write-Note 'Creating the Python environment (1-3 minutes)...'
    Invoke-Checked $Python @('-m', 'venv', (Join-Path $dest 'venv')) 'Creating the Python environment'
    Invoke-Checked $venvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '--no-warn-script-location',
                             '-r', (Join-Path $dest 'server\requirements.txt')) 'Installing Python packages'
    Set-Content -Path (Join-Path $dest '.complete') -Value (Get-Date -Format o)
    Write-Ok "release $Version installed"
    return $dest
}

# ---------------------------------------------------------------- voice and browser packages

# pip --target builds in a temp folder that Python 3.12.4+ on Windows makes readable only by
# the installing account (SYSTEM, admins) and then moves the packages into place, keeping that.
# Kairo's own account then saw faster_whisper as an empty folder (2026-10-14: "cannot import
# name 'WhisperModel' from 'faster_whisper' (unknown location)"). So after every install every
# file and folder inherits C:\Kairo's rights again. Only for folders under tools\ (admins-only):
# never as SYSTEM in a folder Kairo's account can write (a planted junction would redirect it).
function Repair-ToolAcl([string]$Dir) {
    & icacls.exe $Dir /reset /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls /reset failed on $Dir (exit code $LASTEXITCODE)" }
}

# One folder per package list: tools\<kind>-<hash>. The release records which one it uses
# (<release>\<kind>-site.txt), so a rollback also goes back to the matching packages. A folder
# is only used once it is complete, never half-installed. The record is written first: from
# then on the release expects <kind>, so a failed install fails the self-test (= rollback).
function Install-ToolSite($P, [string]$ReleaseDir, [string]$Kind, [string]$What, [scriptblock]$AfterPip) {
    $req = Join-Path $ReleaseDir "server\requirements-$Kind.txt"
    $hash = (Get-FileHash -Algorithm SHA256 $req).Hash.Substring(0, 12).ToLower()
    $site = Join-Path $P.Tools "$Kind-$hash"
    Set-Content -Path (Join-Path $ReleaseDir "$Kind-site.txt") -Value $site -Encoding ASCII
    if (Test-Path (Join-Path $site '.complete')) { return $site }
    foreach ($d in @($site, "$site.new")) { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
    $new = "$site.new"
    $py = Join-Path $ReleaseDir 'venv\Scripts\python.exe'
    Write-Note "Installing $What (once per package list)..."
    Invoke-Checked $py @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '--no-warn-script-location',
                         '--target', $new, '-r', $req) "Installing $What"
    if ($AfterPip) { & $AfterPip $new $py | Out-Host }
    Repair-ToolAcl $new
    Set-Content -Path (Join-Path $new '.complete') -Value (Get-Date -Format o) -Encoding ASCII
    Move-Item $new $site
    return $site
}

# Releases from before 2026-10-14 use the shared tools\voice and tools\browser: repair them once,
# so a rollback to one of them has working packages too.
function Repair-LegacyToolSites($P) {
    foreach ($kind in @('voice', 'browser')) {
        $old = Join-Path $P.Tools $kind
        $mark = Join-Path $old '.acl-repaired'
        if ((Test-Path $old) -and -not (Test-Path $mark)) {
            try { Repair-ToolAcl $old; Set-Content -Path $mark -Value (Get-Date -Format o) -Encoding ASCII }
            catch { Write-Warn "Could not repair $old ($($_.Exception.Message.Trim()))" }
        }
    }
}

# Package folders no kept release points to any more (2 GB each for voice).
function Remove-UnusedToolSites($P) {
    $used = @{}
    $releases = @(Get-ChildItem $P.Releases -Directory -ErrorAction SilentlyContinue)
    foreach ($kind in @('voice', 'browser')) {
        $legacyUsed = $false
        foreach ($r in $releases) {
            $f = Join-Path $r.FullName "$kind-site.txt"
            if (Test-Path $f) { $used[(Get-Content -Raw $f).Trim().ToLower()] = $true } else { $legacyUsed = $true }
        }
        if ($legacyUsed) { $used[(Join-Path $P.Tools $kind).ToLower()] = $true }
    }
    foreach ($d in @(Get-ChildItem $P.Tools -Directory -ErrorAction SilentlyContinue)) {
        if ($d.Name -notmatch '^(voice|browser)(-[0-9a-f]{12}(\.new)?)?$') { continue }
        if ($used.ContainsKey($d.FullName.ToLower())) { continue }
        try { Remove-Item -Recurse -Force $d.FullName; Write-Note "removed unused $($d.Name)" }
        catch { Write-Warn "Could not remove $($d.FullName) ($($_.Exception.Message.Trim()))" }
    }
}

# Phase 3 voice: the big speech packages (about 2 GB with the NVIDIA runtime) in
# tools\voice-<hash>; models in data\models. An install problem never stops the install
# itself: the self-test after the switch finds it and rolls back.
function Install-Voice($P, [string]$ReleaseDir) {
    if ($env:KAIRO_SKIP_VOICE -eq '1') { Write-Note 'Voice skipped (KAIRO_SKIP_VOICE=1).'; return }
    if (-not (Test-Path (Join-Path $ReleaseDir 'server\requirements-voice.txt'))) { return }
    try {
        $site = Install-ToolSite $P $ReleaseDir 'voice' 'voice (speech recognition and voices, about 2 GB)' $null
        Use-KairoEnv $P
        $env:KAIRO_VOICE_SITE = $site
        $py = Join-Path $ReleaseDir 'venv\Scripts\python.exe'
        Push-Location (Join-Path $ReleaseDir 'server')
        try { Invoke-Checked $py @('-m', 'app.voice', 'download') 'Downloading voice models' }
        finally { Pop-Location; Remove-Item Env:KAIRO_VOICE_SITE -ErrorAction SilentlyContinue }
        Write-Ok "Voice ready ($site)"
    } catch {
        Write-Warn "Voice not set up: $($_.Exception.Message.Trim()). The self-test decides (rollback if the release needs it)."
    }
}

# Kairo's own headless browser (Playwright + Chromium, about 300 MB) for reading the school
# portal read-only, in tools\browser-<hash> (Chromium inside it, in ms-playwright).
function Install-Browser($P, [string]$ReleaseDir) {
    if ($env:KAIRO_SKIP_BROWSER -eq '1') { Write-Note 'Browser skipped (KAIRO_SKIP_BROWSER=1).'; return }
    if (-not (Test-Path (Join-Path $ReleaseDir 'server\requirements-browser.txt'))) { return }
    $chromium = {
        param($new, $py)
        $old = $env:PYTHONPATH
        $env:PYTHONPATH = $new
        $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $new 'ms-playwright'
        try { Invoke-Checked $py @('-m', 'playwright', 'install', 'chromium') 'Downloading Chromium' }
        finally { $env:PYTHONPATH = $old; Remove-Item Env:PLAYWRIGHT_BROWSERS_PATH -ErrorAction SilentlyContinue }
    }
    try {
        $site = Install-ToolSite $P $ReleaseDir 'browser' 'Kairo''s browser (school portal, read-only)' $chromium
        Write-Ok "Browser ready ($site)"
    } catch {
        Write-Warn "Browser not set up: $($_.Exception.Message.Trim()). The self-test decides (rollback if the release needs it)."
    }
}

# Highest migration revision in a release (files are named NNNN_slug.py; linear history).
function Get-HeadRevision([string]$ReleaseDir) {
    $dir = Join-Path $ReleaseDir 'server\migrations\versions'
    $revs = Get-ChildItem -Path $dir -Filter '*.py' |
        ForEach-Object { if ($_.Name -match '^(\d+)_') { [int]$Matches[1] } } | Sort-Object
    if (-not $revs) { return $null }
    return '{0:D4}' -f ($revs | Select-Object -Last 1)
}

function Invoke-Alembic($P, [string]$ReleaseDir, [string[]]$Arguments) {
    Use-KairoEnv $P
    $py = Join-Path $ReleaseDir 'venv\Scripts\python.exe'
    Push-Location (Join-Path $ReleaseDir 'server')
    try { & $py -m alembic @Arguments | Out-Host; return ($LASTEXITCODE -eq 0) } finally { Pop-Location }
}

function Backup-Database($P, [string]$Label, [string]$Python) {
    if (-not (Test-Path $P.Db)) { Write-Note 'No database yet, nothing to back up.'; return $null }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $dest = Join-Path $P.Backups "kairo-$Label-$stamp.sqlite"
    $code = 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()'
    Invoke-Checked $Python @('-c', $code, $P.Db, $dest) 'Database backup'
    Get-ChildItem $P.Backups -Filter 'kairo-*.sqlite' | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 20 | Remove-Item -Force
    Write-Ok "database backed up to $dest"
    return $dest
}

function Set-CurrentRelease($P, [string]$Version) {
    $target = Get-ReleaseDir $P $Version
    if (Test-Path $P.Current) { & cmd.exe /c rmdir "$($P.Current)" | Out-Null }  # removes the link only
    [void](New-Item -ItemType Junction -Path $P.Current -Target $target)
}

function Remove-OldReleases($P, $State, [int]$Keep = 3) {
    $keepSet = @($State.current, $State.previous) | Where-Object { $_ }
    $all = Get-ChildItem $P.Releases -Directory | Sort-Object Name -Descending
    $n = 0
    foreach ($d in $all) {
        $n++
        if ($n -le $Keep -or $keepSet -contains $d.Name) { continue }
        Remove-Item -Recurse -Force $d.FullName
        Write-Note "removed old release $($d.Name)"
    }
}

# ---------------------------------------------------------------- run / stop / health

function Get-KairoProcesses($P) {
    $root = $P.Root.TrimEnd('\') + '\'
    return @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine -like '*app.serve*' -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, 'OrdinalIgnoreCase')
    })
}

function Stop-Kairo($P) {
    $task = Get-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue
    if ($task) { Stop-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue }
    foreach ($proc in (Get-KairoProcesses $P)) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 20; $i++) {
        if (@(Get-KairoProcesses $P).Count -eq 0) { break }  # @(): an empty result is $null under StrictMode
        Start-Sleep -Milliseconds 500
    }
}

function Start-Kairo {
    Start-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath
}

function Get-Health {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$script:Port/health" -UseBasicParsing -TimeoutSec 5
        return ($r.Content | ConvertFrom-Json)
    } catch { return $null }
}

function Wait-Healthy([int]$Seconds = 90) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $h = Get-Health
        if ($h -and $h.status -eq 'ok') { return $h }
        Start-Sleep -Seconds 2
    }
    return $null
}

# Boot task: starts at boot as kairo-svc, whether or not anyone is logged in
# (Task Scheduler keeps the account's password; that also gives it the
# "log on as a batch job" right). A second trigger re-checks every 5 minutes;
# IgnoreNew means it only starts Kairo if it isn't running. No time limit.
function Register-KairoTask($P, $Svc) {
    # Task Scheduler history on, so "why didn't Kairo start?" has an answer.
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & wevtutil.exe sl Microsoft-Windows-TaskScheduler/Operational /e:true | Out-Null } catch { } finally { $ErrorActionPreference = $old }
    $py = Join-Path $P.Current 'venv\Scripts\python.exe'
    $taskArgs = "-m app.serve --env-file `"$($P.EnvFile)`""
    $action = New-ScheduledTaskAction -Execute $py -Argument $taskArgs -WorkingDirectory (Join-Path $P.Current 'server')
    $atBoot = New-ScheduledTaskTrigger -AtStartup
    $watchdog = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    [void](Register-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -Action $action `
        -Trigger @($atBoot, $watchdog) -User $Svc.Name -Password $Svc.Password -RunLevel Limited -Settings $settings -Force `
        -Description 'Kairo personal assistant. Starts at boot as kairo-svc, nobody needs to be logged in.')
}

# Boot check: 3 minutes after every boot, as SYSTEM, checks that Kairo, the
# Claude login, Tailscale and the HTTPS address all came back, and writes
# data\logs\boot-check.json (shown on the More screen).
function Register-BootCheckTask($P) {
    $script = Join-Path $P.Installer 'Kairo-BootCheck.ps1'
    $taskArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Root `"$($P.Root)`""
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT3M'
    $principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
    [void](Register-ScheduledTask -TaskName $script:BootCheckTaskName -TaskPath $script:TaskPath -Action $action `
        -Trigger $trigger -Principal $principal -Settings $settings -Force `
        -Description 'Checks after every boot that Kairo, Claude, Tailscale and HTTPS came back.')
}

# Auto-update: every minute, as SYSTEM (it installs programs and restarts
# Kairo). Asks Kairo first; only installs when nothing is running and, unless
# the release is marked [urgent], not in the quiet hours.
function Register-AutoUpdateTask($P) {
    $script = Join-Path $P.Installer 'Kairo-AutoUpdate.ps1'
    $taskArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Root `"$($P.Root)`""
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs
    $trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
    $principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 45) -MultipleInstances IgnoreNew
    [void](Register-ScheduledTask -TaskName $script:AutoUpdateTaskName -TaskPath $script:TaskPath -Action $action `
        -Trigger $trigger -Principal $principal -Settings $settings -Force `
        -Description 'Installs new tested Kairo versions from GitHub when Kairo is idle. Rolls back by itself.')
}

# Start menu -> Kairo: the everyday double-click files.
function Install-StartMenu($P) {
    if (-not (Test-Path $script:StartMenu)) { [void](New-Item -ItemType Directory -Path $script:StartMenu) }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($name in @('Kairo Logins', 'Kairo Check', 'Update Kairo', 'Rollback Kairo', 'Uninstall Kairo')) {
        $lnk = $shell.CreateShortcut((Join-Path $script:StartMenu "$name.lnk"))
        $lnk.TargetPath = Join-Path $P.Root "$name.bat"
        $lnk.WorkingDirectory = $P.Root
        $lnk.Save()
    }
}

function Remove-StartMenu {
    if (Test-Path $script:StartMenu) { Remove-Item -Recurse -Force $script:StartMenu }
}

function Unregister-KairoTask {
    foreach ($n in @($script:TaskName, $script:BootCheckTaskName, $script:AutoUpdateTaskName)) {
        $task = Get-ScheduledTask -TaskName $n -TaskPath $script:TaskPath -ErrorAction SilentlyContinue
        if ($task) { Unregister-ScheduledTask -TaskName $n -TaskPath $script:TaskPath -Confirm:$false }
    }
}

# Why didn't Kairo start? Task result + the end of its log, in plain view.
function Show-StartProblem($P) {
    $info = Get-ScheduledTaskInfo -TaskName $script:TaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue
    if ($info) { Write-Note ("Task 'Kairo': last result 0x{0:X8}, last run {1}" -f $info.LastTaskResult, $info.LastRunTime) }
    try {
        $events = Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' -MaxEvents 200 -ErrorAction Stop |
            Where-Object { $_.Message -like "*\Kairo\Kairo*" } | Select-Object -First 5
        foreach ($e in $events) { Write-Note ("Task Scheduler {0:HH:mm:ss}: {1}" -f $e.TimeCreated, ($e.Message -split "`r?`n")[0]) }
    } catch { }
    $log = Join-Path $P.Logs 'kairo.log'
    if (Test-Path $log) {
        Write-Note 'Last lines of kairo.log:'
        Get-Content $log -Tail 15 | ForEach-Object { Write-Note "  $_" }
    } else {
        Write-Note 'kairo.log was never written: Python did not start (check that Kairo''s account can read Python and C:\Kairo).'
    }
}

# ---------------------------------------------------------------- deploy with auto-rollback

# Installs $SourceApp as the running release. On any failure after the switch:
# schema downgrade (with the new code, which knows every migration), back to
# the previous release, restart. Returns $true on success.
function Deploy-Release($P, [string]$SourceApp, [string]$Python, $Svc, [switch]$Force) {
    $info = Read-ReleaseInfo $SourceApp
    $version = [string]$info.version
    $state = Read-State $P
    $prev = $state.current

    Write-Step "Installing Kairo $version"
    if ($prev -eq $version -and -not $Force) {
        Write-Ok "Kairo $version is already the installed version"
        Register-KairoTask $P $Svc
        if (-not (Get-Health)) { Start-Kairo }
        if (Wait-Healthy 90) { Write-Ok 'Kairo is running'; return $true }
        Write-Bad 'Kairo is installed but not answering. See the log in C:\Kairo\data\logs\kairo.log'
        return $false
    }

    $newDir = Install-ReleaseFiles $P $SourceApp $version $Python
    Repair-LegacyToolSites $P
    Install-Voice $P $newDir
    Install-Browser $P $newDir
    $backup = Backup-Database $P "before-$version" $Python

    Write-Step 'Switching to the new version'
    Stop-Kairo $P
    Set-CurrentRelease $P $version
    Register-KairoTask $P $Svc
    $migrated = Invoke-Alembic $P $newDir @('upgrade', 'head')
    $healthy = $null
    if ($migrated) {
        Start-Kairo
        $healthy = Wait-Healthy 90
    } else {
        Write-Bad 'Database update failed.'
    }

    $selftest = $null
    $selftestOk = $true
    if ($healthy) {
        $selftest = Invoke-ToolSelfTest $P $version
        $selftestOk = Test-SelfTestPassed $selftest
        if (-not $selftestOk) { Write-Bad "Self-test failed: $(Get-SelfTestReason $selftest)" }
        elseif ($selftest -and -not $selftest.ok) { Write-Warn "Self-test: $(Get-SelfTestReason $selftest) (no reason to go back; Kairo shows it on Today)" }
        elseif ($selftest) { Write-Ok 'Self-test passed: tools, safety hook, Claude, voice and browser work for kairo-svc' }
    }

    if ($healthy -and ($selftestOk -or -not $prev)) {
        $state.previous = $prev
        $state.current = $version
        $state.history = @(@($state.history) + $version | Select-Object -Unique)
        Save-State $P $state
        Remove-OldReleases $P $state
        Remove-UnusedToolSites $P
        if ($selftestOk) { Write-Ok "Kairo $version is running"; return $true }
        Write-Bad 'There is no earlier version to go back to: Kairo keeps running, but what failed above does not work.'
        return $false
    }

    if ($healthy) { Write-Bad "Kairo $version started, but its self-test failed." }
    else { Write-Bad "Kairo $version did not start correctly."; Show-StartProblem $P }
    if (-not $prev) {
        Stop-Kairo $P
        Write-Bad 'There is no earlier version to go back to. See C:\Kairo\data\logs\kairo.log'
        return $false
    }
    Write-Step "Going back to $prev"
    Restore-Release $P $newDir $prev $backup
    # The version we went back to checks itself too (and clears the new version's red chip if it works).
    [void](Invoke-ToolSelfTest $P $prev -Quiet)
    return $false
}

# ---------------------------------------------------------------- self-test after every install/update

# Kairo's own check (server/app/selftest.py), run by the app as kairo-svc: the safety hook
# denies and allows, the CLI reports Kairo's tool server as connected, one harmless real tool
# call arrives, and voice and the browser import and work for real. Returns the answer, or a
# toolchain failure when Kairo doesn't answer. Saved to data\updates\selftest.json for the
# update note.
function Invoke-ToolSelfTest($P, [string]$Version, [switch]$Quiet) {
    if (-not $Quiet) { Write-Note 'Self-test (tools, safety hook, Claude, voice, browser; up to 15 minutes)...' }
    $r = $null
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$script:Port/api/selftest/tools?notify=0" -Method Post -Body '{}' `
            -ContentType 'application/json' -UseBasicParsing -TimeoutSec 900
        $r = $resp.Content | ConvertFrom-Json
    } catch {
        $code = $null
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        if ($code -eq 404) { return $null }   # a release from before the self-test existed
        $r = [pscustomobject]@{ ok = $false; category = 'toolchain'; reason = "the self-test did not answer ($($_.Exception.Message.Trim()))"; checks = $null }
    }
    if (-not $Quiet) {
        $dir = Join-Path $P.Data 'updates'
        if (-not (Test-Path $dir)) { [void](New-Item -ItemType Directory -Path $dir) }
        [pscustomobject]@{ version = $Version; at = (Get-Date).ToString('o'); ok = [bool]$r.ok; category = $r.category; reason = [string]$r.reason } |
            ConvertTo-Json | Set-Content -Path (Join-Path $dir 'selftest.json') -Encoding ASCII
    }
    return $r
}

# Rolled back for: tools/hook broken, voice or browser broken, no answer. Not for a Claude
# login or usage limit (an older version can't fix those) or a model that skipped its tool.
function Test-SelfTestPassed($R) {
    if ($null -eq $R) { return $true }
    if ($R.ok) { return $true }
    return @('claude', 'model') -contains [string]$R.category
}

function Get-SelfTestReason($R) {
    if ($null -eq $R) { return 'not available in this version' }
    return [string]$R.reason
}

function Restore-Release($P, [string]$CodeDirForDowngrade, [string]$Version, [string]$Backup) {
    Stop-Kairo $P
    $rev = Get-HeadRevision (Get-ReleaseDir $P $Version)
    $ok = $true
    if ($rev) { $ok = Invoke-Alembic $P $CodeDirForDowngrade @('downgrade', $rev) }
    if (-not $ok -and $Backup -and (Test-Path $Backup)) {
        Write-Warn 'Schema downgrade failed; restoring the database backup taken before the update.'
        Copy-Item -Force $Backup $P.Db
    }
    Set-CurrentRelease $P $Version
    Start-Kairo
    if (Wait-Healthy 90) { Write-Ok "Kairo $Version is running again" }
    else { Write-Bad "Kairo $Version did not come back either. See C:\Kairo\data\logs\kairo.log" }
}

Export-ModuleMember -Function * -Variable TaskName, TaskPath, Port, AutoUpdateTaskName, StartMenu
