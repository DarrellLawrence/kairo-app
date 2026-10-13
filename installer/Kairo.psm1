# Shared functions for the Kairo installer, updater, rollback and uninstaller.
# Windows PowerShell 5.1 compatible. Keep this file ASCII-only.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:TaskName = 'Kairo'
$script:TaskPath = '\Kairo\'
$script:Port = 8000

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
        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -Wait -PassThru
        exit $p.ExitCode
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
        Downloads    = Join-Path $Root 'downloads'
        Data         = $data
        Config       = Join-Path $data 'config'
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
        return [pscustomobject]@{ current = $s.current; previous = $s.previous; history = @($s.history | Where-Object { $_ }) }
    }
    return [pscustomobject]@{ current = $null; previous = $null; history = @() }
}

function Save-State($P, $State) {
    $State | ConvertTo-Json -Depth 4 | Set-Content -Path $P.State -Encoding UTF8
}

function Read-ReleaseInfo([string]$AppDir) {
    return Get-Content -Raw -Path (Join-Path $AppDir 'release.json') | ConvertFrom-Json
}

# ---------------------------------------------------------------- folders & permissions

function Initialize-Folders($P) {
    foreach ($d in @($P.Root, $P.App, $P.Releases, $P.Installer, $P.Downloads, $P.Data, $P.Config, $P.Secrets,
                     $P.Logs, $P.Backups, $P.Workspace, $P.ClaudeConfig, (Split-Path $P.Db))) {
        if (-not (Test-Path $d)) { [void](New-Item -ItemType Directory -Path $d) }
    }
}

# Code: owner can read/run, only admins can change it (so nothing running as the
# owner, the model included, can rewrite Kairo's code). Data: owner full access.
# Everyone else: nothing.
function Set-KairoAcl($P, [string]$Owner) {
    $sys = '*S-1-5-18'; $admins = '*S-1-5-32-544'
    & icacls.exe $P.Root /inheritance:r /grant:r "${sys}:(OI)(CI)F" "${admins}:(OI)(CI)F" "${Owner}:(OI)(CI)RX" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed on $($P.Root)" }
    & icacls.exe $P.Data /grant:r "${Owner}:(OI)(CI)M" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed on $($P.Data)" }
}

function Copy-Tree([string]$From, [string]$To) {
    if (-not (Test-Path $To)) { [void](New-Item -ItemType Directory -Path $To) }
    & robocopy.exe $From $To /E /NFL /NDL /NJH /NJS /NP /R:2 /W:1 | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $From to $To failed (robocopy $LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}

# Owner config: copied on first install only, never overwritten by updates.
function Initialize-Config($P, [string]$AppDir) {
    $src = Join-Path $AppDir 'config'
    Get-ChildItem -Path $src -File | ForEach-Object {
        $dest = Join-Path $P.Config $_.Name
        if (-not (Test-Path $dest)) { Copy-Item $_.FullName $dest; Write-Ok "config $($_.Name) created" }
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

function Find-Python312 {
    if ($env:KAIRO_PYTHON -and (Test-Path $env:KAIRO_PYTHON)) { return $env:KAIRO_PYTHON }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        # No stderr redirect here: in PowerShell 5.1 with ErrorAction Stop it would throw.
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { $exe = & $py.Source -3.12 -c 'import sys; print(sys.executable)' } catch { $exe = $null }
        finally { $ErrorActionPreference = $old }
        if ($LASTEXITCODE -eq 0 -and $exe) { return ([string]$exe).Trim() }
    }
    foreach ($c in @("$env:ProgramFiles\Python312\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
        if (Test-Path $c) { return $c }
    }
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
        if ((Get-KairoProcesses $P).Count -eq 0) { break }
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

# Boot task: starts at boot as the owner WITHOUT a stored password (S4U: runs
# whether or not anyone is logged in; local resources only). A second trigger
# re-checks every 5 minutes; IgnoreNew means it only starts Kairo if it isn't
# running. No time limit.
function Register-KairoTask($P, [string]$UserId) {
    $py = Join-Path $P.Current 'venv\Scripts\python.exe'
    $taskArgs = "-m app.serve --env-file `"$($P.EnvFile)`""
    $action = New-ScheduledTaskAction -Execute $py -Argument $taskArgs -WorkingDirectory (Join-Path $P.Current 'server')
    $atBoot = New-ScheduledTaskTrigger -AtStartup
    $watchdog = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    [void](Register-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -Action $action `
        -Trigger @($atBoot, $watchdog) -Principal $principal -Settings $settings -Force `
        -Description 'Kairo personal assistant. Starts at boot, nobody needs to be logged in.')
}

function Unregister-KairoTask {
    $task = Get-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -Confirm:$false }
}

# ---------------------------------------------------------------- deploy with auto-rollback

# Installs $SourceApp as the running release. On any failure after the switch:
# schema downgrade (with the new code, which knows every migration), back to
# the previous release, restart. Returns $true on success.
function Deploy-Release($P, [string]$SourceApp, [string]$Python, [string]$UserId, [switch]$Force) {
    $info = Read-ReleaseInfo $SourceApp
    $version = [string]$info.version
    $state = Read-State $P
    $prev = $state.current

    Write-Step "Installing Kairo $version"
    if ($prev -eq $version -and -not $Force) {
        Write-Ok "Kairo $version is already the installed version"
        Register-KairoTask $P $UserId
        if (-not (Get-Health)) { Start-Kairo }
        if (Wait-Healthy 90) { Write-Ok 'Kairo is running'; return $true }
        Write-Bad 'Kairo is installed but not answering. See the log in C:\Kairo\data\logs\kairo.log'
        return $false
    }

    $newDir = Install-ReleaseFiles $P $SourceApp $version $Python
    Initialize-Config $P $SourceApp
    $backup = Backup-Database $P "before-$version" $Python

    Write-Step 'Switching to the new version'
    Stop-Kairo $P
    Set-CurrentRelease $P $version
    Register-KairoTask $P $UserId
    $migrated = Invoke-Alembic $P $newDir @('upgrade', 'head')
    $healthy = $null
    if ($migrated) {
        Start-Kairo
        $healthy = Wait-Healthy 90
    } else {
        Write-Bad 'Database update failed.'
    }

    if ($healthy) {
        $state.previous = $prev
        $state.current = $version
        $state.history = @(@($state.history) + $version | Select-Object -Unique)
        Save-State $P $state
        Remove-OldReleases $P $state
        Write-Ok "Kairo $version is running"
        return $true
    }

    Write-Bad "Kairo $version did not start correctly."
    if (-not $prev) {
        Stop-Kairo $P
        Write-Bad 'There is no earlier version to go back to. See C:\Kairo\data\logs\kairo.log'
        return $false
    }
    Write-Step "Going back to $prev"
    Restore-Release $P $newDir $prev $backup
    return $false
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

Export-ModuleMember -Function * -Variable TaskName, TaskPath, Port
