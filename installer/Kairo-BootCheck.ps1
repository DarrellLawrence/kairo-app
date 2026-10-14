# Kairo boot check. Runs 3 minutes after every boot as SYSTEM (task
# "\Kairo\Kairo Boot Check"), with nobody logged in. "Kairo Check.bat" runs
# the same checks on demand. Result: C:\Kairo\data\logs\boot-check.json,
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
$checks = New-Object System.Collections.Generic.List[object]

function Add-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    $checks.Add([pscustomobject]@{ name = $Name; ok = $Ok; detail = $Detail })
    if ($Ok) { Write-Ok "$Name - $Detail" } else { Write-Bad "$Name - $Detail" }
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
    $toolsOk = [bool]$c.hook_deny -and [bool]$c.hook_allow -and [string]$c.mcp -eq 'connected' -and ([bool]$c.tool_call -or [string]$t.category -eq 'model')
    Add-Check 'Kairo tools' $toolsOk $(if ($toolsOk) { 'safety hook, tool server and a real tool call work' } else { [string]$t.reason })
    $claudeOk = [string]$c.claude -eq 'ok'
    Add-Check 'Claude login' $claudeOk $(if ($claudeOk) { "answers ($($t.seconds)s for the whole self-test)" } else { [string]$t.reason })
    foreach ($kind in @('voice', 'browser')) {
        if ($c.PSObject.Properties[$kind] -and $null -ne $c.$kind) {
            $d = $t.details.$kind
            Add-Check $(if ($kind -eq 'voice') { 'Voice' } else { 'Browser' }) ([bool]$c.$kind) $(if ($c.$kind) { 'imports and works for kairo-svc' } else { [string]$d.reason })
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
if (-not (Test-Path $P.Logs)) { [void](New-Item -ItemType Directory -Path $P.Logs) }
$report | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $P.Logs 'boot-check.json') -Encoding UTF8

if ($Interactive) {
    if ($allOk) { Write-Ok 'Everything came back by itself.' } else { Write-Warn 'Something is not right; the lines marked XX say what.' }
    if (-not $CI -and $allOk) { Wait-Enter 'Press Enter to close this window' }
}
exit $(if ($allOk) { 0 } else { 1 })
