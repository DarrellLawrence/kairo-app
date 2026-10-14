# Kairo boot check. Runs 3 minutes after every boot as SYSTEM (task
# "\Kairo\Kairo Boot Check"), with nobody logged in. "Kairo Check.bat" runs
# the same checks on demand. Result: C:\Kairo\status\boot-check.json (fix F24: not data\),
# shown on the iPhone under More -> Last boot check.
param([string]$Root = 'C:\Kairo', [switch]$Interactive, [switch]$CI)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Kairo.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'KairoSetup.psm1') -Force -DisableNameChecking

if ($Interactive) {
    $passArgs = @('-Root', "`"$Root`"", '-Interactive')
    if ($CI) { $passArgs += '-CI' }
    Assert-Admin $PSCommandPath $passArgs
}

$P = Get-KairoPaths $Root
# Fix F24: an update interrupted by the power cut that caused this boot must not leave Kairo's account off.
try { Clear-StaleDataLock $P } catch { Write-Warn "Data lock: $($_.Exception.Message)" }
$checks = New-Object System.Collections.Generic.List[object]
$tailnetLogin = ''
$url = $null

# Level 'warn': nothing is broken, but there is something to do (yellow on the More screen).
function Add-Check([string]$Name, [bool]$Ok, [string]$Detail, [string]$Level = '') {
    $checks.Add([pscustomobject]@{ name = $Name; ok = $Ok; detail = $Detail; level = $Level })
    if (-not $Ok) { Write-Bad "$Name - $Detail" } elseif ($Level -eq 'warn') { Write-Warn "$Name - $Detail" } else { Write-Ok "$Name - $Detail" }
}

if ($Interactive) { Write-Step 'Kairo check' }

# 1. The app answers (give it up to 5 minutes after boot)
$h = Wait-Healthy 300
Add-Check 'Kairo running' ($null -ne $h) $(if ($h) { 'answers on 127.0.0.1:8000' } else { 'no answer after 5 minutes; see data\logs\kairo.log' })

# 2. It runs as Kairo's own limited account
$owners = @(Get-KairoProcesses $P | ForEach-Object { (Invoke-CimMethod -InputObject $_ -MethodName GetOwner).User })
$svcOk = $owners.Count -gt 0 -and @($owners | Where-Object { $_ -ne 'kairo-svc' }).Count -eq 0
Add-Check 'Own account' $svcOk $(if ($owners.Count) { "runs as: $($owners -join ', ')" } else { 'no Kairo process found' })

# 3. Kairo has hands, as kairo-svc: the safety hook blocks, the tool server is connected, one
#    real tool call arrives, Claude answers, voice and the browser work. Kairo pushes a failure.
$t = if ($h) { Invoke-ToolsSelfTest } else { $null }
if ($t -and $t.PSObject.Properties['checks'] -and $t.checks) {
    $c = $t.checks
    $missing = @(if ($c.PSObject.Properties['tools_missing']) { $c.tools_missing } else { @() })
    $toolsOk = [bool]$c.hook_deny -and [bool]$c.hook_allow -and [string]$c.mcp -eq 'connected' -and $missing.Count -eq 0 -and
        ($(if ($c.PSObject.Properties['task_added']) { [bool]$c.task_added } else { [bool]$c.tool_call }))
    Add-Check 'Kairo tools' $toolsOk $(if ($toolsOk) { 'safety hook, tool server, all tools, and a real task added like a chat message' } else { [string]$t.reason })
    if ($c.PSObject.Properties['context']) {
        # An empty vault (new install) is a to-do, not a failure; notes that don't reach the model are.
        $state = [string]$c.context
        $ctxOk = $state -ne 'missing' -and [string]$c.personality -ne 'older'
        $warn = @(if ($t.PSObject.Properties['warnings']) { $t.warnings } else { @() }) | Select-Object -First 1
        $detail = if (-not $ctxOk) { [string]$t.reason } elseif ($state -eq 'empty') { [string]$warn } else { 'Profile, Goals and the current personality reach the model' }
        Add-Check 'Context' $ctxOk $detail $(if ($ctxOk -and $state -eq 'empty') { 'warn' } else { '' })
    }
    $claudeOk = [string]$c.claude -eq 'ok'
    Add-Check 'Claude login' $claudeOk $(if ($claudeOk) { "answers ($($t.seconds)s for the whole self-test)" } else { [string]$t.reason })
    $labels = @{ voice = 'Voice'; browser = 'Browser'; memory = 'Semantic memory' }
    foreach ($kind in @('voice', 'browser', 'memory')) {
        if ($c.PSObject.Properties[$kind] -and $null -ne $c.$kind) {
            $d = $t.details.$kind
            $okText = if ($kind -eq 'memory') { "works for kairo-svc (on $([string]$d.device))" } else { 'imports and works for kairo-svc' }
            Add-Check $labels[$kind] ([bool]$c.$kind) $(if ($c.$kind) { $okText } else { [string]$d.reason })
        }
    }
} else {
    # A release from before the tools self-test: only the Claude login check exists there.
    $r = if ($h) { Invoke-SelfTest } else { $null }
    $claudeOk = ($null -ne $r) -and [bool]$r.ok
    $claudeDetail = if ($claudeOk) { "replied in $($r.seconds)s" } elseif ($r) { "$($r.brain): $($r.error)" } else { 'not tested (Kairo not running)' }
    Add-Check 'Claude login' $claudeOk $claudeDetail
}

if (-not $CI) {
    # 4. Tailscale came back by itself
    $ts = Get-TailscaleStatus
    $tsOk = ($null -ne $ts) -and ($ts.BackendState -eq 'Running')
    Add-Check 'Tailscale' $tsOk $(if ($ts) { "state: $($ts.BackendState)" } else { 'Tailscale not answering' })
    # The PC's own Tailscale account is the owner's: only it may run the diagnosis from the phone.
    try {
        $u = $ts.User.PSObject.Properties[[string]$ts.Self.UserID]
        if ($u -and [string]$u.Value.LoginName -and [string]$u.Value.LoginName -ne 'tagged-devices') { $tailnetLogin = [string]$u.Value.LoginName }
    } catch { }

    # 5. The HTTPS address is still published to Kairo
    $serve = ''
    try { $serve = [string](& (Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe') serve status --json | Out-String) } catch { }
    $serveOk = $serve -match '127\.0\.0\.1:8000'
    Add-Check 'HTTPS publish' $serveOk $(if ($serveOk) { 'tailscale serve -> 127.0.0.1:8000' } else { 'tailscale serve not set; run Kairo Logins' })

    # 6. The HTTPS address works end to end
    $url = Get-KairoUrl
    $httpsOk = $false; $httpsDetail = 'no tailnet address'
    if ($url) {
        try {
            $resp = Invoke-WebRequest -Uri "$url/health" -UseBasicParsing -TimeoutSec 20
            $httpsOk = ($resp.StatusCode -eq 200)
            $httpsDetail = "$url answers"
        } catch { $httpsDetail = "$url not reachable: $($_.Exception.Message)" }
    }
    Add-Check 'HTTPS address' $httpsOk $httpsDetail
}

$allOk = @($checks | Where-Object { -not $_.ok }).Count -eq 0
$report = [pscustomobject]@{
    time   = (Get-Date).ToString('o')
    trigger = $(if ($Interactive) { 'manual' } else { 'boot' })
    all_ok = $allOk
    checks = $checks
}
if ($tailnetLogin) { $report | Add-Member -NotePropertyName tailnet_login -NotePropertyValue $tailnetLogin }
# Fix plan 32: Kairo checks this address itself every hour (is the tailnet path to the iPhone up?).
if ($url) { $report | Add-Member -NotePropertyName kairo_url -NotePropertyValue $url }
if (-not (Test-Path $P.Status)) { [void](New-Item -ItemType Directory -Path $P.Status) }
$report | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $P.Status 'boot-check.json') -Encoding UTF8

if ($Interactive) {
    if ($allOk) { Write-Ok 'Everything came back by itself.' } else { Write-Warn 'Something is not right; the lines marked XX say what.' }
    if (-not $CI -and $allOk) { Wait-Enter 'Press Enter to close this window' }
}
exit $(if ($allOk) { 0 } else { 1 })
