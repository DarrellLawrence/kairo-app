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
    $t = ($Text -replace '\s+', ' ').Trim()
    if ($t.Length -gt 90) { $t = $t.Substring(0, 87) + '...' }
    return $t
}

$exitCode = 0
$held = $false
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
            if ($s.waiting -ne 'no-token') { Write-Log 'No GitHub token: run Kairo Logins once. Auto-update is off until then.'; $s.waiting = 'no-token'; Save-UpdaterState $P $s }
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
        if ($s.waiting -ne $ready.reason) {
            Write-Log "Kairo $latest is ready; waiting ($($ready.reason))."
            $s.waiting = [string]$ready.reason; Save-UpdaterState $P $s
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
        $why = if ($st -and -not $st.ok) { " (self-test: $(Format-Reason $st.reason))" } elseif ($dr.reason) { " ($(Format-Reason $dr.reason))" } else { '' }
        Write-UpdateNote "Kairo update to $latest failed and was undone$why. Still on $now." $false $installed $latest
        Write-Log "Failed: still on $now. $latest will not be tried again automatically."
        $exitCode = 1
    } else {
        $attempts = if ($s.retry -and [string]$s.retry.version -eq $latest) { [int]$s.retry.attempts + 1 } else { 1 }
        $wait = @(10, 30, 120, 360)[[Math]::Min($attempts, 4) - 1]
        $s.retry = @{ version = $latest; attempts = $attempts; next = (Get-Date).AddMinutes($wait).ToString('o') }
        $why = if ($dr -and $dr.reason) { $dr.reason } elseif (Test-Path $err) { ((Get-Content $err | Select-Object -Last 3) -join ' ') } else { "exit code $code" }
        Write-Log "Not installed (attempt $attempts, nothing changed, still on $now): $(Format-Reason $why). Next try in $wait minutes."
        if ($attempts -eq 3) {
            Write-UpdateNote "Kairo $latest could not be installed yet ($(Format-Reason $why)). Still on $now; it keeps trying." $false $installed $latest
        }
        $exitCode = 1
    }
    Save-UpdaterState $P $s
} catch {
    try { Write-Log "Error: $($_.Exception.Message)" } catch { }
    $exitCode = 1
} finally {
    if ($held) { try { [void](Request-Kairo '/api/update/release' $null) } catch { } }
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
