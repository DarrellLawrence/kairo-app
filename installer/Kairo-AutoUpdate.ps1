# Kairo auto-update. Runs every 5 minutes as SYSTEM (task "\Kairo\Kairo Auto-Update").
#
# 1. Asks GitHub for the newest tested release (cheap: an unchanged answer
#    does not count against GitHub's limits).
# 2. Newer than the running version, and not a version that already failed?
#    Asks Kairo whether now is a good moment: no chat or job running, and not
#    in the quiet hours unless the release notes say [urgent]. If not: try
#    again next minute.
# 3. Installs it with "Update Kairo" (backup, health check, automatic
#    rollback) and leaves a one-line note that Kairo pushes to the iPhone.
#
#   -TestRelease <json>  CI only: {"tag_name": "...", "body": "...", "zip": "<path>"}
#   -CI                  CI only: passed on to Update Kairo (no downloads, fake Claude)
param([string]$Root = 'C:\Kairo', [string]$TestRelease = '', [switch]$CI)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Kairo.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'KairoSetup.psm1') -Force -DisableNameChecking

$P = Get-KairoPaths $Root
$statePath = Join-Path $P.Updater 'state.json'
$logPath = Join-Path $P.Updater 'auto-update.log'

function Write-Log([string]$Text) {
    if ((Test-Path $logPath) -and (Get-Item $logPath).Length -gt 1MB) { Move-Item -Force $logPath "$logPath.old" }
    Add-Content -Path $logPath -Value "$(Get-Date -Format 's') $Text" -Encoding ASCII
}

function Read-UpdaterState {
    $s = @{ etag = ''; release = $null; failed = @(); waiting = ''; down_since = '' }
    if (Test-Path $statePath) {
        $j = Get-Content -Raw $statePath | ConvertFrom-Json
        foreach ($k in @('etag', 'release', 'waiting', 'down_since')) { if ($j.PSObject.Properties[$k]) { $s[$k] = $j.$k } }
        if ($j.PSObject.Properties['failed']) { $s.failed = @($j.failed | Where-Object { $_ }) }
    }
    return $s
}

function Save-UpdaterState($S) { $S | ConvertTo-Json -Depth 5 | Set-Content -Path $statePath -Encoding ASCII }

# GET releases/latest with If-None-Match. Returns @{ changed; release; etag }.
function Get-LatestRelease([string]$Repo, [string]$Token, [string]$ETag) {
    $req = [Net.HttpWebRequest]::Create("https://api.github.com/repos/$Repo/releases/latest")
    $req.UserAgent = 'kairo-updater'
    $req.Accept = 'application/vnd.github+json'
    $req.Headers.Add('Authorization', "Bearer $Token")
    if ($ETag) { $req.Headers.Add('If-None-Match', $ETag) }
    $req.Timeout = 20000
    try {
        $resp = $req.GetResponse()
    } catch [Net.WebException] {
        $r = $_.Exception.Response
        if ($r -and [int]$r.StatusCode -eq 304) { $r.Close(); return @{ changed = $false; release = $null; etag = $ETag } }
        throw
    }
    try {
        $reader = New-Object IO.StreamReader($resp.GetResponseStream())
        $json = $reader.ReadToEnd() | ConvertFrom-Json
        $rel = @{ tag_name = [string]$json.tag_name; body = [string]$json.body }
        return @{ changed = $true; release = $rel; etag = [string]$resp.Headers['ETag'] }
    } finally { $resp.Close() }
}

function Request-Kairo([string]$Path, $Body) {
    $json = if ($Body) { $Body | ConvertTo-Json -Compress } else { '{}' }
    return Invoke-RestMethod -Uri "http://127.0.0.1:8000$Path" -Method Post -Body $json -ContentType 'application/json' -TimeoutSec 10 -UseBasicParsing
}

function Write-UpdateNote([string]$Message, [bool]$Ok, [string]$From, [string]$To) {
    $dir = Join-Path $P.Data 'updates'
    if (-not (Test-Path $dir)) { [void](New-Item -ItemType Directory -Path $dir) }
    @{ message = $Message; ok = $Ok; from = $From; to = $To; at = (Get-Date).ToString('o'); notified = $false } |
        ConvertTo-Json | Set-Content -Path (Join-Path $dir 'last-update.json') -Encoding ASCII
}

# The self-test result of this install (Deploy-Release writes it), for the note on the iPhone.
function Read-SelfTest([string]$Version, [datetime]$Since) {
    $f = Join-Path $P.Data 'updates\selftest.json'
    if (-not (Test-Path $f)) { return $null }
    try { $j = Get-Content -Raw $f | ConvertFrom-Json } catch { return $null }
    if ([string]$j.version -ne $Version -or [datetime]$j.at -lt $Since) { return $null }
    return $j
}

function Format-Reason([string]$Text) {
    $t = ($Text -replace '\s+', ' ').Trim()
    if ($t.Length -gt 90) { $t = $t.Substring(0, 87) + '...' }
    return $t
}

$exitCode = 0
$held = $false
try {
    Protect-UpdaterFolder $P
    $installed = (Read-State $P).current
    if (-not $installed) { exit 0 }
    $s = Read-UpdaterState

    $zip = ''
    if ($TestRelease) {
        $t = Get-Content -Raw $TestRelease | ConvertFrom-Json
        $rel = @{ tag_name = [string]$t.tag_name; body = [string]$t.body }
        $zip = [string]$t.zip
    } else {
        $repo = [string](Read-ReleaseInfo $P.Current).update_repo
        $token = Get-GitHubToken $P
        if (-not $token) {
            if ($s.waiting -ne 'no-token') { Write-Log 'No GitHub token: run Kairo Logins once. Auto-update is off until then.'; $s.waiting = 'no-token'; Save-UpdaterState $s }
            exit 0
        }
        $r = Get-LatestRelease $repo $token ([string]$s.etag)
        if ($r.changed) { $s.etag = $r.etag; $s.release = $r.release; Save-UpdaterState $s }
        if (-not $s.release) { exit 0 }
        $rel = @{ tag_name = [string]$s.release.tag_name; body = [string]$s.release.body }
    }

    $latest = $rel.tag_name -replace '^v', ''
    if (-not $latest -or $latest -eq $installed) { exit 0 }
    if (@($s.failed) -contains $latest) { exit 0 }   # failed before: only "Update Kairo" by hand retries it
    $urgent = $rel.body -match '\[urgent\]'

    $ready = $null
    try { $ready = Request-Kairo '/api/update/ready' @{ urgent = [bool]$urgent } } catch { $ready = $null }
    if (-not $ready) {
        # Kairo is not answering. Usually it is just starting (after a reboot): wait.
        # Down for 30 minutes: install anyway, the new version may be the fix.
        if (-not $s.down_since) { $s.down_since = (Get-Date).ToString('o'); Save-UpdaterState $s }
        if (((Get-Date) - [datetime]$s.down_since).TotalMinutes -lt 30 -and -not $urgent) { exit 0 }
        Write-Log 'Kairo has not answered for 30 minutes; installing without asking.'
    } else { $s.down_since = '' }
    if ($ready -and -not $ready.ready) {
        if ($s.waiting -ne $ready.reason) {
            Write-Log "Kairo $latest is ready; waiting ($($ready.reason))."
            $s.waiting = [string]$ready.reason; Save-UpdaterState $s
        }
        exit 0
    }
    $held = [bool]$ready   # Kairo now holds new chats off; released below if we stop early

    Write-Log "Installing Kairo $latest (running: $installed$(if ($urgent) { ', urgent' }))."
    $startedAt = Get-Date
    # A separate process with its output in files: in PowerShell 5.1, redirecting the
    # output of a script in-process turns any program's stderr line (a pip warning)
    # into a fatal error.
    $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'Update-Kairo.ps1')`" -Root `"$Root`" -Auto"
    if ($zip) { $argLine += " -ZipPath `"$zip`"" }
    if ($CI) { $argLine += ' -CI' }
    $out = Join-Path $P.Updater 'last-run.out.txt'
    $err = Join-Path $P.Updater 'last-run.err.txt'
    $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $argLine -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    $code = $proc.ExitCode
    foreach ($f in @($out, $err)) { if (Test-Path $f) { Get-Content $f | ForEach-Object { Write-Log "  $_" } } }
    $now = (Read-State $P).current
    $s.waiting = ''
    $st = Read-SelfTest $latest $startedAt
    if ($code -eq 0 -and $now -eq $latest) {
        $msg = "Kairo updated to $latest."
        if ($st -and -not $st.ok) { $msg += " Self-test: $(Format-Reason $st.reason)" }
        Write-UpdateNote $msg $true $installed $latest
        Write-Log "Done: running $latest. $(if ($st) { "Self-test: ok=$($st.ok) $($st.reason)" })"
        $held = $false
    } else {
        $s.failed = @(@($s.failed) + $latest | Select-Object -Unique)
        $why = if ($st -and -not $st.ok) { " (self-test: $(Format-Reason $st.reason))" } else { '' }
        Write-UpdateNote "Kairo update to $latest failed and was undone$why. Still on $now." $false $installed $latest
        Write-Log "Failed: still on $now. $latest will not be tried again automatically."
        $exitCode = 1
    }
    Save-UpdaterState $s
} catch {
    try { Write-Log "Error: $($_.Exception.Message)" } catch { }
    $exitCode = 1
} finally {
    if ($held) { try { [void](Request-Kairo '/api/update/release' $null) } catch { } }
}
exit $exitCode
