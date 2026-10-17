# Kairo auto-update. Runs every 5 minutes as SYSTEM (task "\Kairo\Kairo Auto-Update").
#
# 1. Asks GitHub for the newest tested release (cheap: an unchanged answer
#    does not count against GitHub's limits).
# 2. Newer than the running version (never older), not a version that ran and
#    had to be undone, not one a manual rollback left (fix P2.13)? Asks Kairo
#    whether now is a good moment: no chat, job, call or browser running, and
#    not in the quiet hours unless the release notes say [urgent]. If not: try
#    again in 5 minutes.
# 3. Installs it with "Update Kairo" (backup, health check, self-test, automatic
#    rollback) and leaves a one-line note that Kairo pushes to the iPhone. A
#    hiccup before the switch (download, network) is retried later (10 min,
#    30 min, 2 h, then every 6 h), never blacklisted.
# 4. Owner's rule 2026-10-17: every update that fails, is undone or is stuck
#    is a push with the reason: each failed try, a release still waiting after
#    3 hours outside the quiet hours, an error in this script that lasts 30
#    minutes (then every 6 hours), no GitHub token.
#
#   -TestRelease <json>  CI only: {"tag_name": "...", "body": "...", "zip": "<path>"}
#   -CI                  CI only: passed on to Update Kairo (no downloads, fake Claude)
param([string]$Root = 'C:\Kairo', [string]$TestRelease = '', [switch]$CI)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Kairo.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'KairoSetup.psm1') -Force -DisableNameChecking

$P = Get-KairoPaths $Root
$logPath = Join-Path $P.Updater 'auto-update.log'

function Write-Log([string]$Text) {
    if ((Test-Path $logPath) -and (Get-Item $logPath).Length -gt 1MB) { Move-Item -Force $logPath "$logPath.old" }
    Add-Content -Path $logPath -Value "$(Get-Date -Format 's') $Text" -Encoding ASCII
}


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

# Fix F24: in C:\Kairo\status (Kairo reads it, only admins change it), never data\: Kairo runs.
function Write-UpdateNote([string]$Message, [bool]$Ok, [string]$From, [string]$To) {
    if (-not (Test-Path $P.Status)) { [void](New-Item -ItemType Directory -Path $P.Status) }
    @{ message = $Message; ok = $Ok; from = $From; to = $To; at = (Get-Date).ToString('o'); notified = $false } |
        ConvertTo-Json | Set-Content -Path (Join-Path $P.Status 'last-update.json') -Encoding ASCII
}

# The self-test result of this install (Deploy-Release writes it), for the note on the iPhone.
function Read-SelfTest([string]$Version, [datetime]$Since) {
    $f = Join-Path $P.Status 'selftest.json'
    if (-not (Test-Path $f)) { return $null }
    try { $j = Get-Content -Raw $f | ConvertFrom-Json } catch { return $null }
    if ([string]$j.version -ne $Version -or [datetime]$j.at -lt $Since) { return $null }
    return $j
}

function Format-Reason([string]$Text) {
    $t = ($Text -replace '\s+', ' ').Trim().TrimEnd('.')
    if ($t.Length -gt 90) { $t = $t.Substring(0, 87) + '...' }
    return $t
}

function Format-Wait([double]$Minutes) {
    if ($Minutes -ge 120) { return "$([int][Math]::Floor($Minutes / 60)) hours" }
    return "$([int]$Minutes) minutes"
}

$StuckMinutes = 180   # outside the quiet hours: a release waiting this long is news
$WaitWhy = @{ 'busy' = 'a chat was running'; 'job running' = 'a background job was running'
              'call live' = 'a call was live'; 'browser open' = 'the school portal browser was open' }

# A try that changed nothing: again later (10 min, 30 min, 2 h, then every 6 h), never blacklisted; each
# try is noted for the push.
function Set-Retry($S, [string]$Version, [string]$Why, [string]$From, [string]$On) {
    $attempts = if ($S.retry -and [string]$S.retry.version -eq $Version) { [int]$S.retry.attempts + 1 } else { 1 }
    $wait = @(10, 30, 120, 360)[[Math]::Min($attempts, 4) - 1]
    $S.retry = @{ version = $Version; attempts = $attempts; next = (Get-Date).AddMinutes($wait).ToString('o') }
    Write-Log "Not installed (attempt $attempts, nothing changed, still on $On): $(Format-Reason $Why). Next try in $wait minutes."
    Write-UpdateNote "Kairo $Version didn't install (try $attempts): $(Format-Reason $Why). Still on $On; next try in $(Format-Wait $wait)." $false $From $Version
}

# An error in this script itself (GitHub not answering, a broken file, ...): a push once it has lasted
# 30 minutes (one bad network minute is not news), then every 6 hours while it lasts.
function Write-ErrorNote([string]$Text) {
    $s = Read-UpdaterState $P
    $at = Get-Date
    $e = $s.error
    $since = if ($e -and $e.since) { [datetime]$e.since } else { $at }
    $noted = if ($e -and $e.noted) { [datetime]$e.noted } else { $null }
    if ((-not $noted -and ($at - $since).TotalMinutes -ge 30) -or ($noted -and ($at - $noted).TotalHours -ge 6)) {
        $on = [string](Read-State $P).current
        Write-UpdateNote "Kairo's auto-update has hit an error for $(Format-Wait ($at - $since).TotalMinutes): $(Format-Reason $Text). Still on $on." $false $on ''
        $noted = $at
    }
    $s.error = @{ text = (Format-Reason $Text); since = $since.ToString('o'); noted = $(if ($noted) { $noted.ToString('o') } else { '' }) }
    Save-UpdaterState $P $s
}

$exitCode = 0
$held = $false
$installing = $false
$errored = $false
try {
    Protect-UpdaterFolder $P
    Clear-StaleDataLock $P   # fix F24: an interrupted update must not leave Kairo's account off
    $installed = (Read-State $P).current
    if (-not $installed) { exit 0 }
    try { Write-TailnetStatus $P } catch { Write-Log "Tailnet status: $($_.Exception.Message)" }   # calls: direct or relayed
    if (-not $TestRelease) {   # the voice lab's Kyutai test, once the owner asked for it in the app
        try { Install-KyutaiIfRequested $P (Join-Path $P.Releases $installed) } catch { Write-Log "Kyutai test: $($_.Exception.Message)" }
        try { Repair-VoiceLLM $P (Join-Path $P.Releases $installed) } catch { Write-Log "Voice model: $($_.Exception.Message)" }
    }
    $s = Read-UpdaterState $P

    $zip = ''
    if ($TestRelease) {
        $t = Get-Content -Raw $TestRelease | ConvertFrom-Json
        $rel = @{ tag_name = [string]$t.tag_name; body = [string]$t.body }
        $zip = [string]$t.zip
    } else {
        $repo = [string](Read-ReleaseInfo $P.Current).update_repo
        $token = Get-GitHubToken $P
        if (-not $token) {
            if ($s.waiting -ne 'no-token') {
                Write-Log 'No GitHub token: run Kairo Logins once. Auto-update is off until then.'
                Write-UpdateNote "Auto-update is off: there is no GitHub token on the PC. On the PC: Start menu -> Kairo -> Kairo Logins, once." $false $installed ''
                $s.waiting = 'no-token'; Save-UpdaterState $P $s
            }
            exit 0
        }
        $r = Get-LatestRelease $repo $token ([string]$s.etag)
        if ($r.changed) { $s.etag = $r.etag; $s.release = $r.release; Save-UpdaterState $P $s }
        if (-not $s.release) { exit 0 }
        $rel = @{ tag_name = [string]$s.release.tag_name; body = [string]$s.release.body }
    }

    $latest = $rel.tag_name -replace '^v', ''
    if (-not $latest -or (Compare-KairoVersion $latest $installed) -le 0) { exit 0 }   # only newer versions
    if (@($s.failed) -contains $latest) { exit 0 }   # ran and was undone: only "Update Kairo" by hand retries it
    if ($s.hold -and (Compare-KairoVersion $latest ([string]$s.hold)) -le 0) { exit 0 }   # a manual rollback left it
    if ($s.retry -and [string]$s.retry.version -eq $latest -and (Get-Date) -lt [datetime]$s.retry.next) { exit 0 }
    $urgent = $rel.body -match '\[urgent\]'

    # CI: a background job may be running for a few seconds; the real task just comes back in 5 minutes.
    $tries = if ($CI) { 30 } else { 1 }
    for ($i = 1; $i -le $tries; $i++) {
        $ready = $null
        try { $ready = Request-Kairo '/api/update/ready' @{ urgent = [bool]$urgent } } catch { $ready = $null }
        if (-not ($ready -and -not $ready.ready -and $ready.reason -ne 'quiet') -or $i -eq $tries) { break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) {
        # Kairo is not answering. Usually it is just starting (after a reboot): wait.
        # Down for 30 minutes: install anyway, the new version may be the fix.
        if (-not $s.down_since) { $s.down_since = (Get-Date).ToString('o'); Save-UpdaterState $P $s }
        if (((Get-Date) - [datetime]$s.down_since).TotalMinutes -lt 30 -and -not $urgent) { exit 0 }
        Write-Log 'Kairo has not answered for 30 minutes; installing without asking.'
    } else { $s.down_since = '' }
    if ($ready -and -not $ready.ready) {
        if ($s.waiting -ne $ready.reason) { Write-Log "Kairo $latest is ready; waiting ($($ready.reason))." }
        $s.waiting = [string]$ready.reason
        if ($ready.reason -ne 'quiet') {     # the quiet hours are a normal wait; anything else adds up
            $pv = $s.pending
            $same = $pv -and [string]$pv.version -eq $latest
            $mins = if ($same) { [double]$pv.minutes } else { 0 }
            if ($same -and $pv.last) { $mins += [Math]::Min(((Get-Date) - [datetime]$pv.last).TotalMinutes, 10) }
            $noted = $same -and [bool]$pv.noted
            if (-not $noted -and $mins -ge $StuckMinutes) {
                $why = if ($WaitWhy.ContainsKey([string]$ready.reason)) { $WaitWhy[[string]$ready.reason] } else { [string]$ready.reason }
                Write-UpdateNote "Kairo $latest has been waiting $(Format-Wait $mins) to install; at the last check $why. Still on $installed." $false $installed $latest
                Write-Log "Still waiting after $(Format-Wait $mins) ($($ready.reason)): noted for the iPhone."
                $noted = $true
            }
            $s.pending = @{ version = $latest; minutes = [Math]::Round($mins, 1); last = (Get-Date).ToString('o'); noted = $noted }
        }
        Save-UpdaterState $P $s
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
    $installing = $true
    $s.pending = $null
    $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $argLine -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    $code = $proc.ExitCode
    foreach ($f in @($out, $err)) { if (Test-Path $f) { Get-Content $f | ForEach-Object { Write-Log "  $_" } } }
    $now = (Read-State $P).current
    $s.waiting = ''
    $st = Read-SelfTest $latest $startedAt
    $dr = Read-DeployResult $P $latest $startedAt
    if ($code -eq 0 -and $now -eq $latest) {
        $msg = "Kairo updated to $latest."
        if ($st -and -not $st.ok) { $msg += " Self-test: $(Format-Reason $st.reason)" }
        Write-UpdateNote $msg $true $installed $latest
        Write-Log "Done: running $latest. $(if ($st) { "Self-test: ok=$($st.ok) $($st.reason)" })"
        $s.retry = $null
        $held = $false
    } elseif ($dr -and $dr.stage -eq 'switched' -and -not (@($dr.warnings) -contains [string]$dr.category)) {
        # It ran and failed its checks: a broken release. (Voice or browser failing because their
        # install hit a hiccup is retried instead.)
        $s.failed = @(@($s.failed) + $latest | Select-Object -Unique)
        $s.retry = $null
        $why = if ($st -and -not $st.ok) { " (self-test: $(Format-Reason $st.reason))" } elseif ($dr.reason) { " ($(Format-Reason $dr.reason))" }
               elseif ($dr.category) { " (the $($dr.category) check failed)" } else { " (exit code $code; the PC's updater\auto-update.log has the details)" }
        Write-UpdateNote "Kairo update to $latest failed and was undone$why. Still on $now." $false $installed $latest
        Write-Log "Failed: still on $now. $latest will not be tried again automatically."
        $exitCode = 1
    } else {
        $tail = if (Test-Path $err) { ((Get-Content $err | Select-Object -Last 3) -join ' ').Trim() } else { '' }
        $why = if ($dr -and $dr.reason) { $dr.reason } elseif ($tail) { $tail } else { "exit code $code" }
        Set-Retry $s $latest $why $installed $now
        $exitCode = 1
    }
    $installing = $false
    Save-UpdaterState $P $s
} catch {
    $errored = $true
    $text = $_.Exception.Message
    try { Write-Log "Error: $text" } catch { }
    try {
        if ($installing) { Set-Retry $s $latest $text $installed ([string](Read-State $P).current); Save-UpdaterState $P $s }
        else { Write-ErrorNote $text }
    } catch { try { Write-Log "Could not note the error: $($_.Exception.Message)" } catch { } }
    $exitCode = 1
} finally {
    if ($held) { try { [void](Request-Kairo '/api/update/release' $null) } catch { } }
    if (-not $errored) {   # a tick without an error: an earlier error is over
        try { $es = Read-UpdaterState $P; if ($es.error) { $es.error = $null; Save-UpdaterState $P $es } } catch { }
    }
    # Owner's report 2026-10-17: an update that never sticks must be visible on the phone (Run diagnosis):
    # what this tick saw, without the GitHub token or anything else from the release body.
    try {
        $st = Read-UpdaterState $P                 # a hashtable: etag, release, failed, waiting, hold, retry
        $tag = if ($st.release) { [string](Get-Prop $st.release 'tag_name') } else { '' }
        [ordered]@{ at = (Get-Date).ToString('o'); installed = [string](Read-State $P).current; latest = $tag
                    waiting = [string]$st.waiting; failed = @($st.failed); retry = $st.retry; hold = [string]$st.hold
                    exit = $exitCode } |
            ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $P.Status 'updater.json') -Encoding ASCII
    } catch { }
}
exit $exitCode
