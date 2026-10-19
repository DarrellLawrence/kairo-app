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
$script:CallFirewallRule = 'Kairo calls (WebRTC, Tailscale only)'
$script:TailnetRanges = @('100.64.0.0/10', 'fd7a:115c:a1e0::/48')

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
        Status       = Join-Path $Root 'status'          # fix F24: what elevated scripts write for Kairo to read
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

# ---------------------------------------------------------------- the auto-updater's memory (updater\state.json)

# failed: versions that were installed and had to be undone (never tried again automatically).
# hold:   a manual rollback left this version; it and older ones are not installed again (fix P2.13).
# retry:  {version, attempts, next}: a hiccup (download, network) is retried later, never blacklisted.
function Read-UpdaterState($P) {
    $s = @{ etag = ''; release = $null; failed = @(); waiting = ''; down_since = ''; hold = ''; retry = $null; pending = $null; error = $null }
    $path = Join-Path $P.Updater 'state.json'
    if (Test-Path $path) {
        # A broken state file would stop every update without a word: put it aside and start fresh (a release
        # that failed before is then tried once more, and noted again if it fails).
        try { $j = Get-Content -Raw $path | ConvertFrom-Json } catch { $j = $null }
        if ($null -eq $j) { Move-Item -Force $path "$path.bad"; return $s }
        foreach ($k in @('etag', 'release', 'waiting', 'down_since', 'hold', 'retry', 'pending', 'error')) { if ($j.PSObject.Properties[$k]) { $s[$k] = $j.$k } }
        if ($j.PSObject.Properties['failed']) { $s.failed = @($j.failed | Where-Object { $_ }) }
    }
    return $s
}

function Save-UpdaterState($P, $S) {
    if (-not (Test-Path $P.Updater)) { Protect-UpdaterFolder $P }
    $S | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $P.Updater 'state.json') -Encoding ASCII
}

function Set-UpdateHold($P, [string]$Version) { $s = Read-UpdaterState $P; $s.hold = $Version; Save-UpdaterState $P $s }
function Clear-UpdateHold($P) { $s = Read-UpdaterState $P; if ($s.hold) { $s.hold = ''; Save-UpdaterState $P $s } }

# Versions are yyyy.MM.dd.HHmm-<sha>[-suffix]. 1: A is newer, -1: older, 0: the same.
function Compare-KairoVersion([string]$A, [string]$B) {
    $pa = ($A -split '-', 2)[0]
    $pb = ($B -split '-', 2)[0]
    $c = 0
    try { $c = ([version]$pa).CompareTo([version]$pb) } catch { $c = [string]::CompareOrdinal($pa, $pb) }
    if ($c -eq 0) { $c = [string]::CompareOrdinal($A, $B) }
    return [Math]::Sign($c)
}

# How far an install got: "prepare" (nothing switched yet) or "switched" (the new version ran and
# was checked). The auto-updater blacklists only a version that ran and failed (fix P2.13).
function Write-DeployResult($P, [string]$Version, [string]$Stage, $Ok, [string]$Category = '', [string]$Reason = '', [string[]]$Warnings = @()) {
    if (-not (Test-Path $P.Updater)) { Protect-UpdaterFolder $P }
    [pscustomobject]@{ version = $Version; stage = $Stage; ok = $Ok; category = $Category; reason = $Reason; warnings = @($Warnings)
                       at = (Get-Date).ToString('o') } |
        ConvertTo-Json | Set-Content -Path (Join-Path $P.Updater 'deploy-result.json') -Encoding ASCII
}

function Read-DeployResult($P, [string]$Version, [datetime]$Since) {
    $f = Join-Path $P.Updater 'deploy-result.json'
    if (-not (Test-Path $f)) { return $null }
    try { $j = Get-Content -Raw $f | ConvertFrom-Json } catch { return $null }
    if ([string]$j.version -ne $Version -or [datetime]$j.at -lt $Since) { return $null }
    return $j
}

function Read-ReleaseInfo([string]$AppDir) {
    return Get-Content -Raw -Path (Join-Path $AppDir 'release.json') | ConvertFrom-Json
}

# ---------------------------------------------------------------- folders & permissions

function Initialize-Folders($P) {
    foreach ($d in @($P.Root, $P.App, $P.Releases, $P.Installer, $P.Tools, $P.Downloads, $P.Status, $P.Data, $P.Vault, $P.Config,
                     $P.Secrets, $P.Logs, $P.Backups, $P.Workspace, $P.ClaudeConfig, (Split-Path $P.Db))) {
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

# ---------------------------------------------------------------- data lock (fix F24)

# data\ is Kairo's own account's to change (Modify). A link (junction or symbolic link) it put
# there could steer a write made with admin or SYSTEM rights anywhere on this PC. So every
# elevated write into data\ happens inside this lock: Kairo's tasks stopped, its account
# disabled (no new logon), none of its programs running, and every link under data\ removed
# (Kairo never makes one). Nothing can then redirect the write or race it. A lock a power cut
# left behind is undone by the next auto-update or boot check (Clear-StaleDataLock).
# Elevated scripts never write into data\ outside it: their notes go to C:\Kairo\status.
function Get-DataLockPath($P) { return Join-Path $P.Updater 'data-lock.json' }

function Get-ServiceTasks {
    return @(Get-ScheduledTask -TaskPath $script:TaskPath -ErrorAction SilentlyContinue |
        Where-Object { ([string]$_.Principal.UserId) -like "*$($script:ServiceUser)" })
}

function Stop-ServiceAccountProcesses {
    for ($i = 0; $i -lt 30; $i++) {
        $left = @(Get-Process -IncludeUserName -ErrorAction SilentlyContinue |
            Where-Object { $_.UserName -and (([string]$_.UserName -split '\\')[-1] -eq $script:ServiceUser) })
        if ($left.Count -eq 0) { return }
        $left | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    throw "Programs of Kairo's account $($script:ServiceUser) keep running, so nothing was changed in data\."
}

# Every junction or symbolic link under $Dir, found without following one. A file that is a
# reparse point for another reason (compressed by Windows) is not a link and stays.
function Find-DataLinks([string]$Dir) {
    $found = New-Object System.Collections.Generic.List[string]
    $todo = New-Object System.Collections.Generic.Stack[string]
    $todo.Push($Dir)
    while ($todo.Count -gt 0) {
        $d = $todo.Pop()
        $entries = @()
        try { $entries = @((New-Object IO.DirectoryInfo $d).GetFileSystemInfos()) }
        catch { Write-Warn "Could not look inside $d ($($_.Exception.Message.Trim()))"; continue }
        foreach ($e in $entries) {
            $isDir = ($e.Attributes -band [IO.FileAttributes]::Directory) -ne 0
            if (($e.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $link = $isDir
                if (-not $isDir) {
                    try { $link = ([string](Get-Item -LiteralPath $e.FullName -Force).LinkType) -eq 'SymbolicLink' } catch { $link = $true }
                }
                if ($link) { $found.Add($e.FullName) }
            } elseif ($isDir) { $todo.Push($e.FullName) }
        }
    }
    return ,$found
}

function Remove-DataLinks($P) {
    if (-not (Test-Path -LiteralPath $P.Data)) { return }
    $top = Get-Item -LiteralPath $P.Data -Force
    if (($top.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$($P.Data) itself is a link: refusing to write through it." }
    foreach ($l in (Find-DataLinks $P.Data)) {
        # Removes the link itself, never what it points to.
        if (([IO.File]::GetAttributes($l) -band [IO.FileAttributes]::Directory) -ne 0) { [IO.Directory]::Delete($l) }
        else { [IO.File]::Delete($l) }
        Write-Warn "Removed a link from Kairo's data folder (Kairo never makes one): $l"
    }
}

# Returns the names of Kairo's tasks that were running (Exit-DataLock starts them again).
function Enter-DataLock($P) {
    if (-not (Test-Path $P.Updater)) { Protect-UpdaterFolder $P }
    $tasks = Get-ServiceTasks
    $running = @($tasks | Where-Object { [string]$_.State -eq 'Running' } | ForEach-Object { [string]$_.TaskName })
    [pscustomobject]@{ pid = $PID; at = (Get-Date).ToString('o'); restart = $running } |
        ConvertTo-Json | Set-Content -Path (Get-DataLockPath $P) -Encoding ASCII
    try {
        Write-PlannedRestart $P
        foreach ($t in $tasks) { Stop-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -ErrorAction SilentlyContinue }
        if (Get-LocalUser -Name $script:ServiceUser -ErrorAction SilentlyContinue) { Disable-LocalUser -Name $script:ServiceUser }
        Stop-ServiceAccountProcesses
        Remove-DataLinks $P
    } catch {
        Exit-DataLock $P $running
        throw
    }
    return ,$running
}

function Exit-DataLock($P, [string[]]$Restart = @()) {
    if (Get-LocalUser -Name $script:ServiceUser -ErrorAction SilentlyContinue) { Enable-LocalUser -Name $script:ServiceUser }
    Remove-Item -Force (Get-DataLockPath $P) -ErrorAction SilentlyContinue
    foreach ($name in @($Restart | Where-Object { $_ })) {
        Start-ScheduledTask -TaskName $name -TaskPath $script:TaskPath -ErrorAction SilentlyContinue
    }
}

# For short elevated writes (Kairo Logins): $Do runs inside the lock; Kairo's tasks start again after.
function Invoke-DataLocked($P, [scriptblock]$Do) {
    $restart = Enter-DataLock $P
    try { & $Do } finally { Exit-DataLock $P $restart }
}

# A lock its installer no longer holds (power cut, killed window): Kairo's account back on.
function Clear-StaleDataLock($P) {
    $f = Get-DataLockPath $P
    if (-not (Test-Path $f)) { return }
    $j = $null
    try { $j = Get-Content -Raw $f | ConvertFrom-Json } catch { }
    if ($j -and (Get-Process -Id ([int]$j.pid) -ErrorAction SilentlyContinue) -and ([datetime]$j.at -gt (Get-Date).AddHours(-2))) { return }
    Write-Warn "A data lock was left behind by an interrupted install or update: Kairo's account is on again."
    $restart = @()
    if ($j -and $j.restart) { $restart = @($j.restart) }
    Exit-DataLock $P $restart
}

# Older setups printed a BitLocker recovery key while install.log was recording: blank it out.
function Hide-RecoveryKeys([string]$LogFile) {
    if (-not (Test-Path -LiteralPath $LogFile)) { return }
    $text = Get-Content -Raw -LiteralPath $LogFile
    $clean = [regex]::Replace($text, '\b\d{6}(-\d{6}){7}\b', '[recovery key removed]')
    $clean = [regex]::Replace($clean, '(?m)^(.*first 6 digits of the recovery key.*?:)\s*\d{6}\s*$', '$1 [removed]')
    if ($clean -ne $text) { Set-Content -LiteralPath $LogFile -Value $clean -NoNewline -Encoding UTF8 }
}

function Test-RunningAsSystem { return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq 'S-1-5-18' }

# ---------------------------------------------------------------- Kairo's own Windows account

function New-RandomPassword {
    $bytes = New-Object byte[] 30
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes); $rng.Dispose()
    return [Convert]::ToBase64String($bytes) + 'aA1!'   # + one of each class for password policies
}

# Secrets only Windows and admins may read (C:\Kairo\updater), machine-encrypted (DPAPI).
function Save-MachineSecret($P, [string]$Name, [string]$Text) {
    Add-Type -AssemblyName System.Security
    if (-not (Test-Path $P.Updater)) { [void](New-Item -ItemType Directory -Path $P.Updater) }
    Protect-UpdaterFolder $P
    $enc = [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($Text), $null,
        [Security.Cryptography.DataProtectionScope]::LocalMachine)
    Set-Content -Path (Join-Path $P.Updater $Name) -Value ([Convert]::ToBase64String($enc)) -Encoding ASCII
}

function Read-MachineSecret($P, [string]$Name) {
    $path = Join-Path $P.Updater $Name
    if (-not (Test-Path $path)) { return $null }
    try {
        Add-Type -AssemblyName System.Security
        $bytes = [Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String((Get-Content -Raw $path).Trim()), $null,
            [Security.Cryptography.DataProtectionScope]::LocalMachine)
        return [Text.Encoding]::UTF8.GetString($bytes)
    } catch { return $null }
}

function Test-LocalPassword([string]$Name, [string]$Plain) {
    try {
        Add-Type -AssemblyName System.DirectoryServices.AccountManagement
        $ctx = New-Object System.DirectoryServices.AccountManagement.PrincipalContext([System.DirectoryServices.AccountManagement.ContextType]::Machine)
        try { return $ctx.ValidateCredentials($Name, $Plain) } finally { $ctx.Dispose() }
    } catch { return $false }
}

# kairo-svc: a standard (non-admin) local account that only Kairo uses. It is hidden from the
# sign-in screen and can only reach C:\Kairo plus what every Windows user can (not your files).
# Its random password is made once and kept machine-encrypted in C:\Kairo\updater (Windows and
# admins only), so updates re-register the tasks with it. Fix P1.9: it used to be replaced on
# every install and update, and Windows then throws away what the account had encrypted for
# itself (school portal login, Google tokens).
function Initialize-ServiceAccount($P) {
    $name = $script:ServiceUser
    $user = Get-LocalUser -Name $name -ErrorAction SilentlyContinue
    $plain = Read-MachineSecret $P 'kairo-svc.bin'
    $secure = $null
    if ($user -and $plain -and (Test-LocalPassword $name $plain)) {
        Write-Ok 'kairo-svc keeps its password (what it encrypted stays readable)'
    } else {
        $plain = New-RandomPassword
        $secure = ConvertTo-SecureString $plain -AsPlainText -Force
        Save-MachineSecret $P 'kairo-svc.bin' $plain
    }
    if (-not $user) {
        [void](New-LocalUser -Name $name -Password $secure -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires `
            -FullName 'Kairo' -Description 'Runs Kairo in the background. No sign-in.')
    } else {
        if ($secure) { Set-LocalUser -Name $name -Password $secure }
        Set-LocalUser -Name $name -PasswordNeverExpires $true -AccountNeverExpires
        Enable-LocalUser -Name $name
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

function Remove-ServiceAccount($P) {
    if ($P) { Remove-Item -Force (Join-Path $P.Updater 'kairo-svc.bin') -ErrorAction SilentlyContinue }
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
    Protect-EnvFile $P
}

# kairo.env: Kairo's own account may read it, only Windows and admins may change it (fix P1.8).
# It used to inherit data\'s rights, so the account could add a line the installer then used.
function Protect-EnvFile($P) {
    & icacls.exe $P.EnvFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' "$($script:ServiceUser):R" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed on $($P.EnvFile)" }
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

# The environment for the pythons the installer runs (as admin or SYSTEM: migrations, voice
# models). Fix P1.8: never the whole kairo.env (a PYTHONPATH= line there was code execution as
# SYSTEM). Paths come from the installer itself; from the file only these model names, and only
# if they look like one; no PYTHON* variable at all.
$script:EnvFromFile = @('KAIRO_WHISPER_MODEL', 'KAIRO_WHISPER_CPU_MODEL', 'KAIRO_KOKORO_VOICE', 'KAIRO_PIPER_VOICE')

function Use-KairoEnv($P) {
    $drop = @(Get-ChildItem Env: | Where-Object { $_.Name -like 'PYTHON*' } | ForEach-Object { $_.Name }) +
            @('KAIRO_DB_URL', 'KAIRO_WORKSPACE_DIR', 'KAIRO_CONFIG_DIR', 'KAIRO_CONFIG_OVERRIDES', 'KAIRO_VOICE_SITE')
    foreach ($name in $drop) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
    $env:KAIRO_DATA_DIR = $P.Data
    $envs = Read-EnvFile $P
    foreach ($k in $script:EnvFromFile) {
        if ($envs.ContainsKey($k) -and $envs[$k] -match '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
            [Environment]::SetEnvironmentVariable($k, $envs[$k], 'Process')
        }
    }
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
# SHA-256 of the files' bytes one after the other (one file: the same as Get-FileHash).
function Get-FilesHash([string[]]$Files) {
    $ms = New-Object System.IO.MemoryStream
    foreach ($f in $Files) { $b = [System.IO.File]::ReadAllBytes($f); $ms.Write($b, 0, $b.Length) }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([System.BitConverter]::ToString($sha.ComputeHash($ms.ToArray())) -replace '-', '') } finally { $sha.Dispose(); $ms.Dispose() }
}

# requirements-<kind>.txt plus its add-ons (requirements-<kind>-*.txt, e.g. voice-gpu), in name order:
# all of them name the folder, so changing any of them installs a clean new one.
function Get-ToolSiteFiles([string]$ReleaseDir, [string]$Kind) {
    $server = Join-Path $ReleaseDir 'server'
    $extra = @(Get-ChildItem -Path $server -Filter "requirements-$Kind-*.txt" -File -ErrorAction SilentlyContinue | Sort-Object Name | ForEach-Object { $_.FullName })
    return @((Join-Path $server "requirements-$Kind.txt")) + $extra
}

# The pip command for a tool folder (also run by CI's voice-install-windows job, so CI installs exactly
# like the PC does). -NoDeps -Upgrade: an add-on that replaces a package already in the folder.
function Invoke-ToolPip([string]$Py, [string]$Target, [string]$Requirements, [string]$What, [switch]$NoDeps) {
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '--no-warn-script-location')
    if ($NoDeps) { $pipArgs += @('--no-deps', '--upgrade') }
    Invoke-Checked $Py ($pipArgs + @('--target', $Target, '-r', $Requirements)) $What
}

function Install-ToolSite($P, [string]$ReleaseDir, [string]$Kind, [string]$What, [scriptblock]$AfterPip) {
    $req = Join-Path $ReleaseDir "server\requirements-$Kind.txt"
    $hash = (Get-FilesHash (Get-ToolSiteFiles $ReleaseDir $Kind)).Substring(0, 12).ToLower()
    $site = Join-Path $P.Tools "$Kind-$hash"
    Set-Content -Path (Join-Path $ReleaseDir "$Kind-site.txt") -Value $site -Encoding ASCII
    if (Test-Path (Join-Path $site '.complete')) { return $site }
    foreach ($d in @($site, "$site.new")) { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
    $new = "$site.new"
    $py = Join-Path $ReleaseDir 'venv\Scripts\python.exe'
    Write-Note "Installing $What (once per package list)..."
    Invoke-ToolPip $py $new $req "Installing $What"
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
    foreach ($kind in @('voice', 'browser', 'memory', 'voicellm')) {
        $legacyUsed = $false
        foreach ($r in $releases) {
            $f = Join-Path $r.FullName "$kind-site.txt"
            if (Test-Path $f) { $used[(Get-Content -Raw $f).Trim().ToLower()] = $true } else { $legacyUsed = $true }
        }
        if ($legacyUsed) { $used[(Join-Path $P.Tools $kind).ToLower()] = $true }
    }
    foreach ($d in @(Get-ChildItem $P.Tools -Directory -ErrorAction SilentlyContinue)) {
        if ($d.Name -notmatch '^(voice|browser|memory|voicellm)(-[0-9a-f]{12}(\.new)?)?$') { continue }
        if ($used.ContainsKey($d.FullName.ToLower())) { continue }
        try { Remove-Item -Recurse -Force $d.FullName; Write-Note "removed unused $($d.Name)" }
        catch { Write-Warn "Could not remove $($d.FullName) ($($_.Exception.Message.Trim()))" }
    }
}

# Phase 3 voice: the big speech packages (about 2 GB with the NVIDIA runtime) in
# tools\voice-<hash>, the models next to them in tools\voice-<hash>\models (fix F24: admins-only,
# never as SYSTEM into data\; Kairo uses them once complete, data\models until then). An install
# problem never stops the install itself: the self-test after the switch finds it and rolls back.
function Install-Voice($P, [string]$ReleaseDir) {
    if ($env:KAIRO_SKIP_VOICE -eq '1') { Write-Note 'Voice skipped (KAIRO_SKIP_VOICE=1).'; return $true }
    if (-not (Test-Path (Join-Path $ReleaseDir 'server\requirements-voice.txt'))) { return $true }
    # Owner's approval 2026-10-17: Kokoro on the GPU. The CUDA build of ONNX Runtime replaces the CPU build
    # the voice packages pulled in (same module); --no-deps: its CUDA DLLs are pinned in the file itself.
    $gpu = {
        param($new, $py)
        $gpuReq = Join-Path $ReleaseDir 'server\requirements-voice-gpu.txt'
        if (Test-Path $gpuReq) { Invoke-ToolPip $py $new $gpuReq 'Installing the voice GPU runtime' -NoDeps }
    }
    try {
        $site = Install-ToolSite $P $ReleaseDir 'voice' 'voice (speech recognition and voices on the GPU, about 3 GB)' $gpu
        $models = Join-Path $site 'models'
        if (-not (Test-Path (Join-Path $models '.complete'))) {
            Use-KairoEnv $P
            $env:KAIRO_VOICE_SITE = $site
            $py = Join-Path $ReleaseDir 'venv\Scripts\python.exe'
            Push-Location (Join-Path $ReleaseDir 'server')
            try { Invoke-Checked $py @('-m', 'app.voice', 'download', '--to', $models) 'Downloading voice models' }
            finally { Pop-Location; Remove-Item Env:KAIRO_VOICE_SITE -ErrorAction SilentlyContinue }
        }
        Write-Ok "Voice ready ($site)"
        return $true
    } catch {
        Write-Warn "Voice not set up: $($_.Exception.Message.Trim()). The self-test decides (rollback if the release needs it)."
        return $false
    }
}

# v8 C6 semantic memory: ONNX Runtime (GPU build, CUDA 12) + tokenizer, about 1.5 GB, and the
# embedding model (Qwen3-Embedding-0.6B, about 1.3 GB) inside the same folder (model\), so the
# download happens in an admins-only folder (never as SYSTEM into data\) and a rollback switches
# the model too. The model revision is in requirements-memory.txt, so a new one = a new folder.
function Install-Memory($P, [string]$ReleaseDir) {
    if ($env:KAIRO_SKIP_MEMORY -eq '1') { Write-Note 'Semantic memory skipped (KAIRO_SKIP_MEMORY=1).'; return $true }
    if (-not (Test-Path (Join-Path $ReleaseDir 'server\requirements-memory.txt'))) { return $true }
    $model = {
        param($new, $py)
        Push-Location (Join-Path $ReleaseDir 'server')
        try { Invoke-Checked $py @('-m', 'app.semantic', 'download', '--to', (Join-Path $new 'model')) 'Downloading the memory model' }
        finally { Pop-Location }
    }
    try {
        Use-KairoEnv $P
        $site = Install-ToolSite $P $ReleaseDir 'memory' 'semantic memory (search by meaning, about 3 GB)' $model
        Write-Ok "Semantic memory ready ($site)"
        return $true
    } catch {
        Write-Warn "Semantic memory not set up: $($_.Exception.Message.Trim()). The self-test decides (rollback if the release needs it)."
        return $false
    }
}

# Local voice (owner's decision 2026-10-16): llama.cpp's server, the candidate voice models and the turn
# detector (server\app\voicellm.json) in tools\voicellm-<hash> (admins only; Kairo's account only reads it).
# Up to about 14 GB, once per voicellm.json; a model that doesn't fit on the disk is skipped (the voice lab
# says so). Partial downloads are kept and checked, so a second try continues. Never a reason to roll back:
# without it the old voice pipeline answers.
function Install-VoiceLLM($P, [string]$ReleaseDir) {
    if ($env:KAIRO_SKIP_VOICELLM -eq '1') { Write-Note 'Local voice model skipped (KAIRO_SKIP_VOICELLM=1).'; return $true }
    $manifest = Join-Path $ReleaseDir 'server\app\voicellm.json'
    if (-not (Test-Path $manifest)) { return $true }
    try {
        $hash = (Get-FileHash -Algorithm SHA256 $manifest).Hash.Substring(0, 12).ToLower()
        $site = Join-Path $P.Tools "voicellm-$hash"
        Set-Content -Path (Join-Path $ReleaseDir 'voicellm-site.txt') -Value $site -Encoding ASCII
        if (Test-Path (Join-Path $site '.complete')) { Write-Ok "Local voice model ready ($site)"; return $true }
        if (Test-Path $site) { Remove-Item -Recurse -Force $site }
        $new = "$site.new"
        Use-KairoEnv $P
        $py = Join-Path $ReleaseDir 'venv\Scripts\python.exe'
        Write-Note 'Downloading the local voice model (llama.cpp and the voice models, up to 14 GB, once)...'
        Push-Location (Join-Path $ReleaseDir 'server')
        try { Invoke-Checked $py @('-m', 'app.voicellm', 'download', '--to', $new) 'Downloading the local voice model' }
        finally { Pop-Location }
        Repair-ToolAcl $new
        Move-Item $new $site
        Write-Ok "Local voice model ready ($site)"
        return $true
    } catch {
        Write-Warn "Local voice model not set up: $($_.Exception.Message.Trim()). Calls use the old voice pipeline."
        return $false
    }
}

# A voice model download that failed during an update (network, disk) is tried again from the auto-update,
# at most every 6 hours, not only with the next release. Partial files are kept and checked, so it continues.
# Kairo picks the voice model up by itself once it is complete (no restart).
function Repair-VoiceLLM($P, [string]$ReleaseDir) {
    if ($env:KAIRO_SKIP_VOICELLM -eq '1') { return }
    $siteFile = Join-Path $ReleaseDir 'voicellm-site.txt'
    if (-not (Test-Path $siteFile)) { return }
    $site = ([string](Get-Content -Raw $siteFile)).Trim()
    if (-not $site -or (Test-Path (Join-Path $site '.complete'))) { return }
    $tried = Join-Path $P.Updater 'voicellm-tried.txt'
    if ((Test-Path $tried) -and ((Get-Date) - (Get-Item $tried).LastWriteTime).TotalHours -lt 6) { return }
    Set-Content -Path $tried -Value (Get-Date -Format o) -Encoding ASCII
    [void](Install-VoiceLLM $P $ReleaseDir)
}

# The voice lab's Kyutai comparison (owner's request 2026-10-16), only once the owner asked for it in the
# app (data\voicelab\kyutai.request; only its existence is read, never its content): its own Python with
# torch + moshi and Kyutai's English models, about 10 GB, in tools\kyutai-<hash> (admins only). Kairo never
# imports it; the lab starts it as a separate program. Tried at most every 6 hours.
function Install-KyutaiIfRequested($P, [string]$ReleaseDir) {
    $flag = Join-Path $P.Data 'voicelab\kyutai.request'
    if (-not (Test-Path -LiteralPath $flag -PathType Leaf)) { return }
    $tried = Join-Path $P.Updater 'kyutai-tried.txt'
    if ((Test-Path $tried) -and ((Get-Date) - (Get-Item $tried).LastWriteTime).TotalHours -lt 6) { return }
    $py = Join-Path $ReleaseDir 'venv\Scripts\python.exe'
    $server = Join-Path $ReleaseDir 'server'
    $manifest = Join-Path $server 'app\voicellm.json'
    if (-not (Test-Path $py) -or -not (Test-Path $manifest)) { return }
    Push-Location $server
    try { $site = ([string](& $py -m app.voicellm kyutai-dir --tools $P.Tools | Select-Object -Last 1)).Trim() } finally { Pop-Location }
    if (-not $site -or (Test-Path (Join-Path $site '.complete'))) { return }
    Set-Content -Path $tried -Value (Get-Date -Format o) -Encoding ASCII
    $k = (Get-Content -Raw $manifest | ConvertFrom-Json).kyutai
    $new = "$site.new"
    try {
        Write-Note 'Installing the Kyutai test for the voice lab (about 10 GB, once)...'
        $kpy = Join-Path $new 'venv\Scripts\python.exe'
        if (-not (Test-Path $kpy)) { Invoke-Checked $py @('-m', 'venv', (Join-Path $new 'venv')) 'Making the Kyutai Python' }
        Invoke-Checked $kpy @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', "torch==$($k.torch)",
                              '--index-url', [string]$k.torch_index) 'Installing torch for Kyutai'
        Invoke-Checked $kpy @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', "moshi==$($k.moshi)") 'Installing moshi'
        Invoke-Checked $kpy @('-I', (Join-Path $server 'tools\kyutai_bench.py'), 'download', $new, $manifest) 'Downloading Kyutai''s models'
        Repair-ToolAcl $new
        Set-Content -Path (Join-Path $new '.complete') -Value (Get-Date -Format o) -Encoding ASCII
        Move-Item $new $site
        Write-Ok "Kyutai test ready ($site)"
    } catch {
        Write-Warn "Kyutai test not installed: $($_.Exception.Message.Trim()). Tried again in 6 hours."
    }
}

# Kairo's own headless browser (Playwright + Chromium, about 300 MB) for reading the school
# portal read-only, in tools\browser-<hash> (Chromium inside it, in ms-playwright).
function Install-Browser($P, [string]$ReleaseDir) {
    if ($env:KAIRO_SKIP_BROWSER -eq '1') { Write-Note 'Browser skipped (KAIRO_SKIP_BROWSER=1).'; return $true }
    if (-not (Test-Path (Join-Path $ReleaseDir 'server\requirements-browser.txt'))) { return $true }
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
        return $true
    } catch {
        Write-Warn "Browser not set up: $($_.Exception.Message.Trim()). The self-test decides (rollback if the release needs it)."
        return $false
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

# The copy taken by Backup-Database replaces the database. A journal left next to it would be
# replayed into the restored file, so it goes too.
function Restore-DatabaseBackup($P, [string]$Backup) {
    foreach ($ext in @('-journal', '-wal', '-shm')) { Remove-Item -Force "$($P.Db)$ext" -ErrorAction SilentlyContinue }
    Copy-Item -Force $Backup $P.Db
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

# The Kairo server. venv\Scripts\python.exe only launches the base Python with the same command line,
# so the server itself runs from outside C:\Kairo: it is found by its command line too (app.serve with
# this install's env file). Owner's report 2026-10-19: an old server outlived an update.
function Get-KairoProcesses($P) {
    $root = $P.Root.TrimEnd('\') + '\'
    $envFile = [string]$P.EnvFile
    return @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine -like '*app.serve*' -and (
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, 'OrdinalIgnoreCase')) -or
            $_.CommandLine.IndexOf($envFile, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    })
}

# A Python still listening on Kairo's port: an old server nobody tracks any more. Other programs are left
# alone (and named), Kairo can't start next to them anyway.
function Get-PortServers {
    $found = @()
    $conns = @()
    try { $conns = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $script:Port -State Listen -ErrorAction Stop) } catch { }
    foreach ($id in @($conns | ForEach-Object { [int]$_.OwningProcess } | Select-Object -Unique)) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
        if ($proc) { $found += $proc }
    }
    return @($found)
}

# Kairo counts its starts to tell a crash loop (fix plan 32); a stop the installer makes on purpose
# is noted in C:\Kairo\status, so the start after it doesn't count.
function Write-PlannedRestart($P) {
    if (-not (Test-Path $P.Status)) { [void](New-Item -ItemType Directory -Path $P.Status) }
    @{ at = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json |
        Set-Content -Path (Join-Path $P.Status 'planned-restart.json') -Encoding ASCII
}

function Stop-Kairo($P) {
    Write-PlannedRestart $P
    $task = Get-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue
    if ($task) { Stop-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 20; $i++) {
        $left = @(Get-KairoProcesses $P) + @(Get-PortServers | Where-Object { $_.Name -like 'python*.exe' })   # @(): $null under StrictMode
        if ($left.Count -eq 0) { return }
        foreach ($proc in $left) {
            if ($i -eq 0) { Write-Note "stopping Kairo (pid $($proc.ProcessId), $($proc.ExecutablePath))" }
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 500
    }
    foreach ($proc in (Get-PortServers)) { Write-Warn "Port $script:Port is still held by $($proc.Name) (pid $($proc.ProcessId), $($proc.ExecutablePath))." }
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

# $Version: only this version counts (its /health says which version answers; older ones don't say).
function Wait-Healthy([int]$Seconds = 90, [string]$Version = '') {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $h = Get-Health
        if ($h -and $h.status -eq 'ok' -and (-not $Version -or [string](Get-Prop $h 'version') -eq $Version)) { return $h }
        Start-Sleep -Seconds 2
    }
    return $null
}

# Starts Kairo and waits until THIS version answers. Owner's report 2026-10-19: an update said "ok" while
# the old server kept answering on the port (the new one could not start next to it). Another version
# still answering is stopped once and Kairo started again; still not this version = not healthy.
function Start-KairoVersion($P, [string]$Version) {
    Start-Kairo
    $h = Wait-Healthy 90 $Version
    if ($h) { return $h }
    $other = Get-Health
    if ($other) {
        $seen = [string](Get-Prop $other 'version')
        Write-Warn "Kairo $(if ($seen) { $seen } else { '(an older version)' }) is still answering instead of $Version (pid $(Get-Prop $other 'pid')): stopping it."
        Stop-Kairo $P
        Start-Kairo
        $h = Wait-Healthy 90 $Version
    }
    return $h
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
# C:\Kairo\status\boot-check.json (shown on the More screen; fix F24: not data\).
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

# Auto-update: every 5 minutes (fix P2.16), as SYSTEM (it installs programs and restarts
# Kairo). Asks Kairo first; only installs when nothing is running and, unless
# the release is marked [urgent], not in the quiet hours.
function Register-AutoUpdateTask($P) {
    $script = Join-Path $P.Installer 'Kairo-AutoUpdate.ps1'
    $taskArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Root `"$($P.Root)`""
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs
    $trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
    $principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 45) -MultipleInstances IgnoreNew
    [void](Register-ScheduledTask -TaskName $script:AutoUpdateTaskName -TaskPath $script:TaskPath -Action $action `
        -Trigger $trigger -Principal $principal -Settings $settings -Force `
        -Description 'Installs new tested Kairo versions from GitHub when Kairo is idle. Rolls back by itself.')
}

# Voice V3 (2026-10-16): a call's audio comes straight from the phone over WebRTC (UDP). Windows lets it
# in to Kairo's Python only from tailnet addresses (Kairo itself also offers only its tailnet address).
# The venv's python.exe starts this machine-wide Python, which is the one that listens. Never fails an
# install: without the rule a call falls back to the old line.
function Set-CallFirewall([string]$Python) {
    if (-not $Python -or -not (Test-Path $Python)) { return }
    try {
        Get-NetFirewallRule -DisplayName $script:CallFirewallRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        [void](New-NetFirewallRule -DisplayName $script:CallFirewallRule -Direction Inbound -Action Allow -Program $Python `
            -Protocol UDP -RemoteAddress $script:TailnetRanges `
            -Description 'Kairo calls: the phone''s audio over WebRTC, only from Tailscale addresses.')
        Write-Ok 'Calls over WebRTC allowed from your tailnet only'
    } catch { Write-Warn "Firewall rule for calls not set ($($_.Exception.Message.Trim())); calls use the old line." }
}

function Remove-CallFirewall {
    Get-NetFirewallRule -DisplayName $script:CallFirewallRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}

function Get-Prop($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $prop = $Object.PSObject.Properties[$Name]
    if ($prop) { return $prop.Value } else { return $null }
}

# Voice V3 (2026-10-16): how each device on the tailnet reaches this PC, direct or through Tailscale's
# relay (C:\Kairo\status\tailnet.json, read by app\tailnet.py). Kairo's own account may not ask
# Tailscale; SYSTEM does it on every auto-update tick. Only what a call needs: no public addresses.
# Also Kairo's own https://<name>.ts.net address (kairo_url): the health watch calls it every hour,
# also when the boot check that records it too ran days ago (acceptance #30).
function Write-TailnetStatus($P, [string]$Json = '') {
    if (-not $Json) {
        $ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
        if (-not (Test-Path $ts)) { return }
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { $Json = [string](& $ts status --json 2>$null | Out-String) } finally { $ErrorActionPreference = $old }
        if (-not $Json.Trim()) { return }
    }
    $st = $Json | ConvertFrom-Json
    $peers = @()
    $all = Get-Prop $st 'Peer'
    if ($all) {
        foreach ($entry in $all.PSObject.Properties) {
            $peer = $entry.Value
            $cur = [string](Get-Prop $peer 'CurAddr')
            $lan = $null
            if ($cur) {
                $h = ($cur -replace ':\d+$', '').Trim('[', ']')
                $lan = [bool]($h -match '^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|fe80:|fd)')
            }
            $peers += [ordered]@{ ips = @(Get-Prop $peer 'TailscaleIPs' | Select-Object -First 4); os = [string](Get-Prop $peer 'OS')
                                  online = [bool](Get-Prop $peer 'Online'); direct = [bool]$cur; lan = $lan
                                  relay = [string](Get-Prop $peer 'Relay'); peer_relay = [bool](Get-Prop $peer 'PeerRelay') }
        }
    }
    $me = Get-Prop $st 'Self'
    $out = [ordered]@{ at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); self_relay = [string](Get-Prop $me 'Relay')
                       peers = @($peers) }
    $dns = ([string](Get-Prop $me 'DNSName')).TrimEnd('.')
    if ($dns) { $out['kairo_url'] = 'https://' + $dns }
    if (-not (Test-Path $P.Status)) { [void](New-Item -ItemType Directory -Path $P.Status) }
    $file = Join-Path $P.Status 'tailnet.json'
    ConvertTo-Json -InputObject $out -Depth 5 | Set-Content -Path "$file.new" -Encoding ASCII
    Move-Item -Force "$file.new" $file
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
# $InLock: the installer's own writes into data\ (rights, kairo.env, sync settings), run inside the
# data lock with the backup and the migration (fix F24), so Kairo stops only once.
function Deploy-Release($P, [string]$SourceApp, [string]$Python, $Svc, [switch]$Force, [scriptblock]$InLock) {
    Set-CallFirewall $Python
    $info = Read-ReleaseInfo $SourceApp
    $version = [string]$info.version
    $state = Read-State $P
    $prev = $state.current

    Write-Step "Installing Kairo $version"
    if ($prev -eq $version -and -not $Force) {
        Write-Ok "Kairo $version is already the installed version"
        if ($InLock) {
            Stop-Kairo $P
            $restart = Enter-DataLock $P
            try { & $InLock | Out-Host } finally { Exit-DataLock $P $restart }
        }
        Register-KairoTask $P $Svc
        $h = Get-Health
        if ($h -and [string](Get-Prop $h 'version') -eq $version) { Write-Ok 'Kairo is running'; return $true }
        if ($h) { Stop-Kairo $P }   # not this version answering (an old server): it goes
        if (Start-KairoVersion $P $version) { Write-Ok 'Kairo is running'; return $true }
        Write-Bad 'Kairo is installed but not answering. See the log in C:\Kairo\data\logs\kairo.log'
        return $false
    }

    Write-DeployResult $P $version 'prepare' $null
    $newDir = Install-ReleaseFiles $P $SourceApp $version $Python
    Repair-LegacyToolSites $P
    $warnings = @()
    if (-not (Install-Voice $P $newDir)) { $warnings += 'voice' }
    if (-not (Install-Browser $P $newDir)) { $warnings += 'browser' }
    if (-not (Install-Memory $P $newDir)) { $warnings += 'memory' }
    if (-not (Install-VoiceLLM $P $newDir)) { $warnings += 'voicellm' }
    Install-KyutaiIfRequested $P $newDir

    Write-Step 'Switching to the new version'
    Write-DeployResult $P $version 'switched' $null '' '' $warnings
    Stop-Kairo $P
    $backup = $null
    $migrated = $false
    $restart = Enter-DataLock $P
    try {
        if ($InLock) { & $InLock | Out-Host }
        $backup = Backup-Database $P "before-$version" $Python
        $migrated = Invoke-Alembic $P $newDir @('upgrade', 'head')
    } finally { Exit-DataLock $P @($restart | Where-Object { $_ -ne $script:TaskName }) }   # Kairo: after the switch, never the old code before it
    Set-CurrentRelease $P $version
    Register-KairoTask $P $Svc
    $healthy = $null
    if ($migrated) {
        Stop-Kairo $P   # anything Task Scheduler started meanwhile (the 5-minute watchdog, a restart) ran the old code
        $healthy = Start-KairoVersion $P $version
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
        elseif ($selftest) { Write-Ok 'Self-test passed: tools, safety hook, Claude, voice, browser and memory work for kairo-svc' }
    }

    if ($healthy -and ($selftestOk -or -not $prev)) {
        $state.previous = $prev
        $state.current = $version
        $state.history = @(@($state.history) + $version | Select-Object -Unique)
        Save-State $P $state
        Remove-OldReleases $P $state
        Remove-UnusedToolSites $P
        Write-DeployResult $P $version 'switched' $selftestOk $(if ($selftest) { [string]$selftest.category } else { '' }) `
            $(if ($selftest) { [string]$selftest.reason } else { '' }) $warnings
        if ($selftestOk) { Write-Ok "Kairo $version is running"; return $true }
        Write-Bad 'There is no earlier version to go back to: Kairo keeps running, but what failed above does not work.'
        return $false
    }

    if ($healthy) { Write-Bad "Kairo $version started, but its self-test failed." }
    else { Write-Bad "Kairo $version did not start correctly."; Show-StartProblem $P }
    Write-DeployResult $P $version 'switched' $false $(if ($selftest) { [string]$selftest.category } else { 'start' }) `
        $(if ($selftest) { [string]$selftest.reason } elseif ($migrated) { 'did not start' } else { 'database update failed' }) $warnings
    if (-not $prev) {
        Stop-Kairo $P
        Write-Bad 'There is no earlier version to go back to. See C:\Kairo\data\logs\kairo.log'
        return $false
    }
    Write-Step "Going back to $prev"
    # A migration that failed half way can't be trusted to downgrade (SQLite keeps what it did):
    # the backup taken before the update comes back instead (fix P2.15).
    Restore-Release $P $newDir $prev $backup -UseBackup:(-not $migrated)
    # The version we went back to checks itself too (and clears the new version's red chip if it works).
    [void](Invoke-ToolSelfTest $P $prev -Quiet)
    return $false
}

# ---------------------------------------------------------------- self-test after every install/update

# Kairo's own check (server/app/selftest.py), run by the app as kairo-svc: the safety hook
# denies and allows, the CLI reports Kairo's tool server as connected, one harmless real tool
# call arrives, and voice and the browser import and work for real. Returns the answer, or a
# toolchain failure when Kairo doesn't answer. Saved to C:\Kairo\status\selftest.json for the
# update note (fix F24: not into data\, Kairo is running).
function Invoke-ToolSelfTest($P, [string]$Version, [switch]$Quiet) {
    if (-not $Quiet) { Write-Note 'Self-test (tools, safety hook, Claude, voice, browser, memory; up to 15 minutes)...' }
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
        if (-not (Test-Path $P.Status)) { [void](New-Item -ItemType Directory -Path $P.Status) }
        [pscustomobject]@{ version = $Version; at = (Get-Date).ToString('o'); ok = [bool]$r.ok; category = $r.category; reason = [string]$r.reason } |
            ConvertTo-Json | Set-Content -Path (Join-Path $P.Status 'selftest.json') -Encoding ASCII
    }
    return $r
}

# Rolled back for: tools/hook broken, voice or browser broken, no answer. Not for what an older
# version can't fix either: Claude login or usage limit, a model that didn't act, an empty vault,
# an old personality.md the owner kept, a voice part off the GPU (owner's request 2026-10-19: red and
# pushed every time; going back would only hold the fix up). Those stay red on Today and are pushed.
function Test-SelfTestPassed($R) {
    if ($null -eq $R) { return $true }
    if ($R.ok) { return $true }
    return @('claude', 'model', 'context', 'config', 'gpu') -contains [string]$R.category
}

function Get-SelfTestReason($R) {
    if ($null -eq $R) { return 'not available in this version' }
    return [string]$R.reason
}

# $BackupWith: a python to take a database backup first (Rollback Kairo), inside the data lock.
function Restore-Release($P, [string]$CodeDirForDowngrade, [string]$Version, [string]$Backup, [switch]$UseBackup, [string]$BackupWith = '') {
    Stop-Kairo $P
    $restart = Enter-DataLock $P
    try {
        if ($BackupWith) { $Backup = Backup-Database $P 'before-rollback' $BackupWith }
        $ok = $false
        if (-not $UseBackup) {
            $rev = Get-HeadRevision (Get-ReleaseDir $P $Version)
            $ok = $true
            if ($rev) { $ok = Invoke-Alembic $P $CodeDirForDowngrade @('downgrade', $rev) }
            if (-not $ok) { Write-Warn 'Schema downgrade failed.' }
        }
        if (-not $ok) {
            if ($Backup -and (Test-Path $Backup)) {
                Write-Warn "Restoring the database backup taken before the update ($Backup)."
                Restore-DatabaseBackup $P $Backup
            } else { Write-Bad 'There is no database backup from before the update to restore.' }
        }
    } finally { Exit-DataLock $P @($restart | Where-Object { $_ -ne $script:TaskName }) }
    Set-CurrentRelease $P $Version
    Stop-Kairo $P
    Start-Kairo
    if (Wait-Healthy 90) { Write-Ok "Kairo $Version is running again" }
    else { Write-Bad "Kairo $Version did not come back either. See C:\Kairo\data\logs\kairo.log" }
}

Export-ModuleMember -Function * -Variable TaskName, TaskPath, Port, AutoUpdateTaskName, StartMenu
