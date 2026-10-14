# CI end-to-end test of the Windows installer on a GitHub Windows runner.
# Not shipped in the release zip. Runs as admin (the runner is admin).
param([Parameter(Mandatory)][string]$Zip, [Parameter(Mandatory)][string]$FakeClaude)

$ErrorActionPreference = 'Stop'
$Root = 'C:\KairoCI'
Import-Module (Join-Path $PSScriptRoot '..\Kairo.psm1') -Force -DisableNameChecking
# The fake CLI must live where Kairo's own account can read it (like the real one, in tools\).
[void](New-Item -ItemType Directory -Force (Join-Path $Root 'tools'))
Copy-Item -Force $FakeClaude (Join-Path $Root 'tools\fake_claude.py')
$FakeClaude = Join-Path $Root 'tools\fake_claude.py'
$work = Join-Path $env:RUNNER_TEMP 'kairo-ci'
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
[void](New-Item -ItemType Directory $work)

function Check([bool]$Cond, [string]$Msg) {
    if (-not $Cond) { Write-Host "FAIL: $Msg" -ForegroundColor Red; throw "FAIL: $Msg" }
    Write-Host "PASS: $Msg" -ForegroundColor Green
}
function Copy-Setup([string]$From, [string]$Name, [string]$Version) {
    $dest = Join-Path $work $Name
    Copy-Item -Recurse $From $dest
    $rj = Join-Path $dest 'app\release.json'
    $info = Get-Content -Raw $rj | ConvertFrom-Json
    $info.version = $Version
    $info | ConvertTo-Json | Set-Content -Path $rj -Encoding UTF8
    return $dest
}
function Invoke-Install([string]$Setup) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Setup 'installer\Install-Kairo.ps1') -CI -Root $Root -ClaudeBin $FakeClaude | Out-Host
    return $LASTEXITCODE
}
function Get-StateObj { return (Get-Content -Raw (Join-Path $Root 'app\state.json') | ConvertFrom-Json) }
function Get-HealthObj {
    try { return ((Invoke-WebRequest 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 5).Content | ConvertFrom-Json) } catch { return $null }
}
function Get-Tables {
    # Script file, not -c: PowerShell 5.1 strips embedded double quotes from native arguments.
    $py = Join-Path $Root 'app\current\venv\Scripts\python.exe'
    $script = Join-Path $work 'tables.py'
    Set-Content -Path $script -Encoding ASCII -Value @(
        'import sqlite3, sys',
        'rows = sqlite3.connect(sys.argv[1]).execute("select name from sqlite_master where type = ''table''")',
        'print(",".join(r[0] for r in rows))'
    )
    $out = [string](& $py $script (Join-Path $Root 'data\db\kairo.sqlite'))
    if ($LASTEXITCODE -ne 0) { throw "table probe failed: $out" }
    return @($out.Trim() -split ',')
}

# Runs a .bat like a double-click would (cmd.exe, no PowerShell in between),
# with an empty stdin so its "press any key" returns. Returns exit code + output.
function Invoke-Bat([string]$Bat, [string]$BatArgs, [string]$KairoRoot) {
    $in = Join-Path $work 'empty-stdin.txt'; Set-Content -Path $in -Value '' -Encoding ASCII
    $out = Join-Path $work 'bat-out.txt'; $err = Join-Path $work 'bat-err.txt'
    $env:KAIRO_ROOT = $KairoRoot
    # A real double-click has no inherited execution policy; drop the one this CI shell may pass on.
    $savedPolicy = $env:PSExecutionPolicyPreference
    Remove-Item Env:\PSExecutionPolicyPreference -ErrorAction SilentlyContinue
    try {
        $proc = Start-Process -FilePath $env:ComSpec -ArgumentList "/d /c `"`"$Bat`" $BatArgs`"" -NoNewWindow -Wait -PassThru `
            -RedirectStandardInput $in -RedirectStandardOutput $out -RedirectStandardError $err
    } finally {
        Remove-Item Env:\KAIRO_ROOT
        if ($savedPolicy) { $env:PSExecutionPolicyPreference = $savedPolicy }
    }
    $text = ((Get-Content -Raw $out -ErrorAction SilentlyContinue) + (Get-Content -Raw $err -ErrorAction SilentlyContinue))
    Write-Host $text
    return [pscustomobject]@{ Code = $proc.ExitCode; Output = [string]$text }
}
function Set-Blocked([string]$Dir) {
    # What a browser + "Extract All" does: every file marked as from the internet.
    Get-ChildItem -LiteralPath $Dir -Recurse -File | ForEach-Object {
        Set-Content -LiteralPath $_.FullName -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3" -Encoding ASCII
    }
}
function Get-BlockedCount([string]$Dir) {
    return @(Get-ChildItem -LiteralPath $Dir -Recurse -File | Where-Object { Get-Item -LiteralPath $_.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue }).Count
}

# ---- first install: double-click "Install Kairo.bat" from Downloads, in a
# folder with spaces and parentheses, files blocked, strict execution policy.
$downloads = Join-Path $env:USERPROFILE 'Downloads\Kairo Setup (1)'
if (Test-Path -LiteralPath $downloads) { Remove-Item -LiteralPath $downloads -Recurse -Force }
[void](New-Item -ItemType Directory -Force $downloads)
Expand-Archive -LiteralPath $Zip -DestinationPath $downloads -Force
$s1 = (Get-ChildItem -LiteralPath $downloads -Directory | Select-Object -First 1).FullName
$v1 = (Get-Content -Raw (Join-Path $s1 'app\release.json') | ConvertFrom-Json).version
# What kairo-home has: full config copies from its first install (fewer actions), one owner edit.
# (-Encoding UTF8: Windows PowerShell would read the umlauts in profile.yaml as ANSI.)
$cfg = Join-Path $Root 'data\config'
[void](New-Item -ItemType Directory -Force $cfg)
Get-Content -Encoding UTF8 (Join-Path $s1 'app\config\permissions.yaml') | Where-Object { $_ -notmatch '^\s+(calendar\.tag|gmail\.send_self):' } |
    Set-Content -Path (Join-Path $cfg 'permissions.yaml') -Encoding UTF8
(Get-Content -Encoding UTF8 (Join-Path $s1 'app\config\profile.yaml')) -replace '^context: .*$', 'context: "ci owner context"' |
    Set-Content -Path (Join-Path $cfg 'profile.yaml') -Encoding UTF8
Copy-Item (Join-Path $s1 'app\config\personality.md') (Join-Path $cfg 'personality.md')
Set-Blocked $s1
Check ((Get-BlockedCount $s1) -gt 10) 'setup files are marked as downloaded from the internet'
$oldPolicy = Get-ExecutionPolicy -Scope CurrentUser
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy AllSigned -Force -ErrorAction SilentlyContinue
Check ((Get-ExecutionPolicy -Scope CurrentUser) -eq 'AllSigned') 'strict execution policy (AllSigned) for this user'
try {
    $r = Invoke-Bat (Join-Path $s1 'Install Kairo.bat') "-CI -ClaudeBin `"$FakeClaude`"" $Root
} finally { Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy $oldPolicy -Force -ErrorAction SilentlyContinue }
Check ($r.Code -eq 0) "first install via 'Install Kairo.bat' from '$s1' exits 0"
Check ((Get-BlockedCount $s1) -eq 0) 'the .bat unblocked its own files'
$log = Join-Path $Root 'install.log'
Check ((Test-Path $log) -and ((Get-Content -Raw $log) -match 'Kairo setup')) "setup log written to $log"
$logIds = @((Get-Acl $log).Access | ForEach-Object { $_.IdentityReference.Value })
Check (@($logIds | Where-Object { $_ -match 'kairo-svc' }).Count -eq 0) "Kairo's own account cannot read the setup log"

# ---- vault sync: runs as kairo-svc, never listens beyond Tailscale/loopback, shares only the vault
$syncTask = Get-ScheduledTask -TaskName 'Kairo Sync' -TaskPath '\Kairo\' -ErrorAction SilentlyContinue
Check ($syncTask -and $syncTask.Principal.UserId -match 'kairo-svc$') 'sync task runs as kairo-svc'
[xml]$stc = Get-Content -Raw (Join-Path $Root 'data\syncthing\config.xml')
$o = $stc.configuration.options
Check ($o.globalAnnounceEnabled -eq 'false' -and $o.localAnnounceEnabled -eq 'false' -and $o.relaysEnabled -eq 'false' -and $o.natEnabled -eq 'false') 'sync: no discovery, relays or NAT traversal'
$la = @($o.listenAddress)
Check ($la.Count -ge 1 -and @($la | Where-Object { $_ -notmatch '://(127\.0\.0\.1|100\.\d+\.\d+\.\d+):22000$' }).Count -eq 0) "sync listens only on loopback/Tailscale ($($la -join ', '))"
Check ($stc.configuration.gui.address -eq '127.0.0.1:8384') 'sync admin page only on this PC'
$vf = $stc.configuration.SelectSingleNode("folder[@id='kairo-vault']")
Check ($vf -and $vf.path -eq (Join-Path $Root 'data\vault') -and @($stc.configuration.SelectNodes('folder')).Count -eq 1) 'sync shares exactly one folder: the vault'
$stProc = $null
for ($i = 0; $i -lt 30 -and -not $stProc; $i++) {
    $stProc = Get-CimInstance Win32_Process -Filter "Name = 'syncthing.exe'" | Where-Object { $_.ExecutablePath -like "$Root\*" } | Select-Object -First 1
    if (-not $stProc) { Start-Sleep -Seconds 2 }
}
Check ($stProc -and (Invoke-CimMethod -InputObject $stProc -MethodName GetOwner).User -eq 'kairo-svc') 'syncthing is running as kairo-svc'
Import-Module (Join-Path $Root 'installer\KairoSync.psm1') -Force -DisableNameChecking
$KPs = Get-KairoPaths $Root
$apiUp = Wait-SyncApi $KPs 60
Check ($null -ne $apiUp -and $apiUp.myID) 'sync API answers on 127.0.0.1'
$other = Join-Path $work 'st-other'
$ErrorActionPreference = 'Continue'   # Windows PowerShell 5.1: redirected stderr would throw
& $env:KAIRO_SYNCTHING generate "--home=$other" 2>&1 | Out-Null
$ErrorActionPreference = 'Stop'
[xml]$oc = Get-Content -Raw (Join-Path $other 'config.xml')
$otherId = $oc.configuration.SelectSingleNode('device').GetAttribute('id')
Add-SyncDevice $KPs $otherId 'CI laptop'
Add-SyncDevice $KPs $otherId 'CI laptop'   # twice: harmless
$fold = Invoke-SyncApi $KPs 'GET' '/rest/config/folders/kairo-vault'
Check (@($fold.devices | Where-Object { $_.deviceID -eq $otherId }).Count -eq 1) 'a paired device gets the vault folder (once)'
$badId = $false; try { Add-SyncDevice $KPs 'not-an-id' 'x' } catch { $badId = $true }
Check $badId 'a wrong device ID is refused'
$fw = @(Get-NetFirewallRule -DisplayName 'Kairo Sync (Tailscale only)' | Get-NetFirewallAddressFilter)
Check ($fw.Count -eq 2 -and @($fw | Where-Object { $_.RemoteAddress -ne '100.64.0.0/10' -and $_.RemoteAddress -ne '100.64.0.0/255.192.0.0' }).Count -eq 0) 'firewall: sync port open only to Tailscale addresses'
Check (Test-Path (Join-Path $Root 'data\vault\10 Profile')) 'vault folders created'

# ---- the .bat never closes on an error: it says why, logs it, waits for a key
$broken = Join-Path $env:USERPROFILE 'Downloads\Kairo Setup broken (2)'
$failRoot = 'C:\Kairo CI fail'
foreach ($d in @($broken, $failRoot)) { if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Recurse -Force } }
Copy-Item -LiteralPath $s1 -Destination $broken -Recurse
Remove-Item -LiteralPath (Join-Path $broken 'installer\KairoSetup.psm1')
$r = Invoke-Bat (Join-Path $broken 'Install Kairo.bat') '-CI' $failRoot
Check ($r.Code -ne 0) 'a broken setup reports failure'
Check ($r.Output -match 'did NOT finish' -and $r.Output -match 'Press any key') 'a broken setup keeps the window open with a plain message'
Check ($r.Output -match 'KairoSetup\.psm1') 'the message names what is wrong'
$failLog = Join-Path $failRoot 'install.log'
Check ((Test-Path -LiteralPath $failLog) -and ((Get-Content -Raw -LiteralPath $failLog) -match 'KairoSetup\.psm1')) 'the error is in install.log'
$lonely = Join-Path $env:USERPROFILE 'Downloads\only the bat'
if (Test-Path -LiteralPath $lonely) { Remove-Item -LiteralPath $lonely -Recurse -Force }
[void](New-Item -ItemType Directory -Force $lonely)
Copy-Item -LiteralPath (Join-Path $s1 'Install Kairo.bat') -Destination $lonely
$r = Invoke-Bat (Join-Path $lonely 'Install Kairo.bat') '' $failRoot
Check ($r.Code -ne 0 -and $r.Output -match 'Extract All') 'a .bat without its files says to extract the zip first'
# The GitHub "Source code" zip has the .bat one folder too deep; it must say so.
$r = Invoke-Bat (Join-Path $PSScriptRoot '..\Install Kairo.bat') '' $failRoot
Check ($r.Code -ne 0 -and $r.Output -match 'Source code' -and $r.Output -match 'Kairo-Setup-') 'the Source code download says to get the Kairo-Setup zip instead'
foreach ($d in @($broken, $failRoot, $lonely)) { Remove-Item -LiteralPath $d -Recurse -Force }
Check ((Get-StateObj).current -eq $v1) "running version is $v1"
$h = Get-HealthObj
Check ($h -ne $null -and $h.status -eq 'ok') 'health: ok'
Check ($h.brain -eq 'ok') 'health: brain ok (fake CLI found)'
$task = Get-ScheduledTask -TaskName 'Kairo' -TaskPath '\Kairo\'
Check ($task.Principal.UserId -match 'kairo-svc$') "boot task runs as Kairo's own account (kairo-svc)"
Check ($task.Principal.LogonType -eq 'Password') 'boot task runs whether or not anyone is logged in'
Check ($task.Principal.RunLevel -eq 'Limited') 'boot task runs without admin rights'
$svcUser = Get-LocalUser -Name 'kairo-svc'
Check ($svcUser.Enabled) 'kairo-svc account exists'
$admins = (Get-LocalGroup -SID 'S-1-5-32-544').Name
Check (-not (Get-LocalGroupMember -Group $admins -Member 'kairo-svc' -ErrorAction SilentlyContinue)) 'kairo-svc is not an administrator'
$owners = @(Get-KairoProcesses (Get-KairoPaths $Root) | ForEach-Object { (Invoke-CimMethod -InputObject $_ -MethodName GetOwner).User })
Check ($owners.Count -ge 1 -and @($owners | Where-Object { $_ -ne 'kairo-svc' }).Count -eq 0) "the running app belongs to kairo-svc ($($owners -join ','))"
$boot = Get-ScheduledTask -TaskName 'Kairo Boot Check' -TaskPath '\Kairo\'
Check ($boot.Principal.UserId -eq 'SYSTEM' -or $boot.Principal.UserId -eq 'S-1-5-18') 'boot check task runs as SYSTEM'
Check ([string]$boot.Triggers[0].Delay -eq 'PT3M') 'boot check runs 3 minutes after boot'
Check (@($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' }).Count -eq 1) 'boot trigger present'
Check ($task.Settings.ExecutionTimeLimit -eq 'PT0S') 'no time limit on the task'
Check ($task.State -eq 'Running') 'task is running'
$listen = @(Get-NetTCPConnection -LocalPort 8000 -State Listen)
Check ($listen.Count -ge 1 -and @($listen | Where-Object { $_.LocalAddress -ne '127.0.0.1' }).Count -eq 0) 'listens on 127.0.0.1 only'
$ids = @((Get-Acl (Join-Path $Root 'data')).Access | ForEach-Object { $_.IdentityReference.Value })
Check (@($ids | Where-Object { $_ -match '\\Users$|^Everyone$|Authenticated Users' }).Count -eq 0) 'data folder: no access for other users'
$codeAcl = (Get-Acl (Join-Path $Root 'app')).Access | Where-Object { $_.IdentityReference.Value -eq [Security.Principal.WindowsIdentity]::GetCurrent().Name }
Check (@($codeAcl | Where-Object { $_.FileSystemRights -match 'Write|Modify|FullControl' }).Count -eq 0) 'code folder: owner cannot modify (read/run only)'
Check ((Test-Path (Join-Path $Root 'Update Kairo.bat')) -and (Test-Path (Join-Path $Root 'Kairo Check.bat'))) 'Update/Rollback/Uninstall/Check .bat files placed in the Kairo folder'
Check ((Invoke-Install $s1) -eq 0) 'running the same version again is harmless'

# ---- config in layers (fix P0.4): new actions arrive, the owner's edit stays, older versions keep a full copy
$eff = Join-Path $Root 'data\config-effective'
Check ((Get-Content -Raw (Join-Path $eff 'permissions.yaml')) -match 'calendar\.tag' -and (Get-Content -Raw (Join-Path $eff 'permissions.yaml')) -match 'gmail\.send_self') 'the running config has the actions the old copy lacked'
Check ((Get-Content -Raw (Join-Path $eff 'profile.yaml')) -match 'ci owner context') "the owner's own edit is in the running config"
$own = Get-Content -Raw (Join-Path $cfg 'profile.yaml')
Check ($own -match 'ci owner context' -and $own -notmatch 'timezone') 'data\config\profile.yaml now holds only the owner''s change'
Check (-not (Test-Path (Join-Path $cfg 'personality.md')) -and (Test-Path (Join-Path $cfg 'README.txt'))) 'unchanged defaults left data\config; a README explains it'
Check (@(Get-ChildItem (Join-Path $Root 'data\backups') -Directory -Filter 'config-before-overrides-*').Count -eq 1) 'the full copies were backed up first'
Check ((Get-Content -Raw (Join-Path $Root 'data\config-legacy\permissions.yaml')) -notmatch 'calendar\.tag') 'older versions keep their full copy (data\config-legacy)'
$cu = Get-Content -Raw (Join-Path $Root 'data\updates\config-update.json') | ConvertFrom-Json
Check ($cu.message -match '^Config updated: 2 new actions') "one note for the iPhone: $($cu.message)"
$envText = Get-Content -Raw (Join-Path $Root 'data\kairo.env')
Check ($envText -match 'KAIRO_CONFIG_OVERRIDES=.*data\\config\r?\n' -and $envText -match 'KAIRO_CONFIG_DIR=.*config-legacy') 'kairo.env: owner changes for this version, the full copy for older ones'

# ---- Kairo has hands (fix P0): the self-test the installer ran after the switch, as kairo-svc
$stj = Get-Content -Raw (Join-Path $Root 'data\updates\selftest.json') | ConvertFrom-Json
Check ($stj.version -eq $v1 -and $stj.ok) "the install ran the self-test and it passed ($($stj.reason))"
$tools = (Invoke-WebRequest 'http://127.0.0.1:8000/api/selftest/tools?notify=0' -Method Post -Body '{}' -ContentType 'application/json' -UseBasicParsing -TimeoutSec 900).Content | ConvertFrom-Json
Check ($tools.ok) "tools self-test passes ($($tools.reason))"
Check ($tools.checks.hook_deny -and $tools.checks.hook_allow) 'the shipped safety hook blocks a forbidden tool (exit 2) and lets a Kairo tool through'
Check ($tools.checks.mcp -eq 'connected') "the CLI reports Kairo's tool server as connected"
Check ($tools.checks.tool_call) 'one real tool call went through mcp_kairo to the backend'
Check ($tools.checks.browser -eq $true) "Kairo's browser imports and renders a page as kairo-svc ($($tools.details.browser.reason))"
$siteFile = Join-Path $Root "app\releases\$v1\browser-site.txt"
$site = if (Test-Path $siteFile) { (Get-Content -Raw $siteFile).Trim() } else { '' }
Check ($site -match '\\tools\\browser-[0-9a-f]{12}$' -and (Test-Path (Join-Path $site '.complete'))) "the release points to its own complete browser folder ($site)"
$pwAcl = @((Get-Acl (Join-Path $site 'playwright\__init__.py')).Access | ForEach-Object { $_.IdentityReference.Value })
Check (@($pwAcl | Where-Object { $_ -match 'kairo-svc' }).Count -ge 1) "kairo-svc may read the installed packages ($($pwAcl -join ', '))"

# ---- what can Kairo's account see? (fake CLI probe, run by the app as kairo-svc)
$canary = Join-Path $env:USERPROFILE 'kairo-canary.txt'
Set-Content -Path $canary -Value 'private'
$probe = Join-Path $Root 'data\workspace\probe.json'
$envFile = Join-Path $Root 'data\kairo.env'
Add-Content -Path $envFile -Value "KAIRO_HOOK_FAKE_PROBE=$probe"
Add-Content -Path $envFile -Value "KAIRO_HOOK_FAKE_PROBE_PATHS=$canary;$envFile"
$KP = Get-KairoPaths $Root
Stop-Kairo $KP; Start-Kairo
Check ($null -ne (Wait-Healthy 90)) 'restarted with the probe on'
$st = (Invoke-WebRequest 'http://127.0.0.1:8000/api/selftest' -Method Post -UseBasicParsing -TimeoutSec 60).Content | ConvertFrom-Json
Check ($st.ok) 'self-test: the CLI runs and answers for kairo-svc'
$pr = Get-Content -Raw $probe | ConvertFrom-Json
Check ($pr.user -eq 'kairo-svc') "the CLI runs as kairo-svc ($($pr.user))"
Check ($pr.admin -eq $false) 'the CLI has no admin rights'
Check (-not $pr.readable.$canary) "kairo-svc cannot read a file in the installer user's profile"
Check ($pr.readable.$envFile) 'kairo-svc can read its own data folder'
$lines = Get-Content $envFile | Where-Object { $_ -notmatch '^KAIRO_HOOK_FAKE_PROBE' }
Set-Content -Path $envFile -Value $lines -Encoding UTF8
Stop-Kairo $KP; Start-Kairo
Check ($null -ne (Wait-Healthy 90)) 'restarted without the probe'

# ---- nothing Kairo's account writes can run as SYSTEM or in an admin window (fix P1.8)
$envAcl = @((Get-Acl $envFile).Access | Where-Object { $_.IdentityReference.Value -match 'kairo-svc' })
Check ($envAcl.Count -ge 1 -and @($envAcl | Where-Object { $_.FileSystemRights -match 'Write|Modify|FullControl|Delete' }).Count -eq 0) "kairo.env: Kairo's account can read it, not change it"
$envTest = Join-Path $work 'envtest.ps1'
@'
param([string]$Installer, [string]$TestRoot)
Import-Module (Join-Path $Installer 'Kairo.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $Installer 'KairoSetup.psm1') -Force -DisableNameChecking
$P = Get-KairoPaths $TestRoot
[void](New-Item -ItemType Directory -Force (Split-Path $P.EnvFile))
Set-Content -Path $P.EnvFile -Encoding ASCII -Value @('PYTHONPATH=C:\evil', 'KAIRO_WHISPER_MODEL=small', 'KAIRO_KOKORO_VOICE=x & calc',
    'KAIRO_DATA_DIR=C:\elsewhere', 'KAIRO_DB_URL=sqlite:///C:/Windows/x.db')
$env:PYTHONSTARTUP = 'C:\evil.py'
Use-KairoEnv $P
$envOk = (-not $env:PYTHONPATH) -and (-not $env:PYTHONSTARTUP) -and $env:KAIRO_WHISPER_MODEL -eq 'small' -and
    (-not $env:KAIRO_KOKORO_VOICE) -and $env:KAIRO_DATA_DIR -eq $P.Data -and (-not $env:KAIRO_DB_URL)
$good = Join-Path $P.Tools 'claude\9.9.9\claude.exe'
$bad = Join-Path $P.Data 'claude.exe'
foreach ($f in @($good, $bad)) { [void](New-Item -ItemType Directory -Force (Split-Path $f)); Set-Content -Path $f -Value 'x' }
$exeOk = (Resolve-ClaudeExe $P $bad) -eq $good -and (Resolve-ClaudeExe $P (Join-Path $P.Tools 'claude\..\..\data\claude.exe')) -eq $good
Write-Host "env ok: $envOk, claude.exe ok: $exeOk"
if ($envOk -and $exeOk) { exit 0 } else { exit 1 }
'@ | Set-Content -Path $envTest -Encoding ASCII
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $envTest -Installer (Join-Path $Root 'installer') -TestRoot (Join-Path $work 'envroot') | Out-Host
Check ($LASTEXITCODE -eq 0) 'the installer takes only model names from kairo.env, and admin windows only start tools\claude\claude.exe'

# ---- the boot check (what runs after every reboot), minus the Tailscale parts
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'installer\Kairo-BootCheck.ps1') -Root $Root -CI | Out-Host
Check ($LASTEXITCODE -eq 0) 'boot check passes (Kairo up, own account, tools, Claude, browser)'
$bc = Get-Content -Raw (Join-Path $Root 'data\logs\boot-check.json') | ConvertFrom-Json
$names = @($bc.checks | ForEach-Object { $_.name })
$missing = @(@('Kairo running', 'Own account', 'Kairo tools', 'Claude login', 'Browser') | Where-Object { $names -notcontains $_ })
Check ($bc.all_ok -and $missing.Count -eq 0) "boot-check.json: all green ($($names -join ', '))"
$status = (Invoke-WebRequest 'http://127.0.0.1:8000/api/status' -UseBasicParsing).Content | ConvertFrom-Json
Check ($status.boot_check.all_ok) 'the app shows the boot check result (More screen)'

# ---- what Kairo's account encrypted for itself survives an update (fix P1.9)
$put = Invoke-WebRequest 'http://127.0.0.1:8000/api/logins/olat' -Method Put -Body '{"username": "ci-user", "password": "ci-pass"}' `
    -ContentType 'application/json' -UseBasicParsing
Check ($put.StatusCode -eq 200) 'school portal login saved (encrypted for kairo-svc)'
$pwSet = (Get-LocalUser -Name 'kairo-svc').PasswordLastSet
Check (Test-Path (Join-Path $Root 'updater\kairo-svc.bin')) "kairo-svc's password is kept machine-encrypted in updater\"

# ---- update keeps owner config
Add-Content (Join-Path $Root 'data\config\profile.yaml') '# ci-marker'
$v2 = "$v1-ci2"
$s2 = Copy-Setup $s1 'v2' $v2
Check ((Invoke-Install $s2) -eq 0) 'update to v2 exits 0'
Check ((Get-LocalUser -Name 'kairo-svc').PasswordLastSet -eq $pwSet) "the update did not replace kairo-svc's password"
$olat = @(((Invoke-WebRequest 'http://127.0.0.1:8000/api/logins' -UseBasicParsing).Content | ConvertFrom-Json) | Where-Object { $_.site -eq 'olat' })[0]
Check ($olat.username -eq 'ci-user' -and $olat.password_set -and $olat.encrypted) 'the school portal login is still readable after the update'
$st = Get-StateObj
Check ($st.current -eq $v2 -and $st.previous -eq $v1) 'v2 running, v1 kept for rollback'
Check ((Get-Content -Raw (Join-Path $Root 'data\config\profile.yaml')) -match 'ci-marker') 'owner config untouched by the update'
Check ((Get-Content -Raw (Join-Path $Root 'data\config-effective\profile.yaml')) -match 'ci owner context') 'the owner''s edit survives the update'

# ---- broken update with a schema change: must roll back by itself
$v3 = "$v1-ci3-broken"
$s3 = Copy-Setup $s1 'v3' $v3
$mig = @"
from alembic import op
import sqlalchemy as sa
revision = "0099"
down_revision = "$((Get-ChildItem (Join-Path $s3 'app\server\migrations\versions') -Filter '*.py' | Sort-Object Name | Select-Object -Last 1).Name.Split('_')[0])"
branch_labels = None
depends_on = None
def upgrade():
    op.create_table("ci_probe", sa.Column("id", sa.Integer, primary_key=True))
def downgrade():
    op.drop_table("ci_probe")
"@
Set-Content -Path (Join-Path $s3 'app\server\migrations\versions\0099_ci_probe.py') -Value $mig -Encoding ASCII
Add-Content -Path (Join-Path $s3 'app\server\app\main.py') -Value "`nraise RuntimeError('ci: deliberately broken release')"
Check ((Invoke-Install $s3) -ne 0) 'broken update reports failure'
Check ((Get-StateObj).current -eq $v2) 'still on v2 after the failed update'
$h = Get-HealthObj
Check ($h -ne $null -and $h.status -eq 'ok') 'v2 answers again after the automatic rollback'
$tables = Get-Tables
Check ($tables -contains 'tasks') 'table probe works (sees the tasks table)'
Check (-not ($tables -contains 'ci_probe')) 'schema change of the broken release was undone'


# ---- an update without Kairo's tool bridge (what every release before 2026-10-14 was): the
#      self-test after the switch fails, the update rolls back by itself and says why
$v5 = "$v1-ci5-notools"
$s5 = Copy-Setup $s1 'v5' $v5
Remove-Item -Recurse -Force (Join-Path $s5 'app\server\mcp_kairo')
Check ((Invoke-Install $s5) -ne 0) 'an update without the tool bridge reports failure'
Check ((Get-StateObj).current -eq $v2) 'still on v2 after the tool-less update'
Check ((Get-HealthObj).status -eq 'ok') 'v2 answers again after rolling back the tool-less update'
$stj = Get-Content -Raw (Join-Path $Root 'data\updates\selftest.json') | ConvertFrom-Json
Check ($stj.version -eq $v5 -and -not $stj.ok -and $stj.category -eq 'toolchain') "the reason is recorded for the update note ($($stj.reason))"

# ---- manual rollback
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'installer\Rollback-Kairo.ps1') -CI -Root $Root | Out-Host
Check ($LASTEXITCODE -eq 0) 'rollback exits 0'
Check ((Get-StateObj).current -eq $v1) 'rolled back to v1'
Check ((Get-HealthObj).status -eq 'ok') 'v1 answers after rollback'

# ---- update from a local zip through "Update Kairo"
$zip2 = Join-Path $work "Kairo-Setup-$v2.zip"
Compress-Archive -Path $s2 -DestinationPath $zip2
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'installer\Update-Kairo.ps1') -CI -Root $Root -ZipPath $zip2 | Out-Host
Check ($LASTEXITCODE -eq 0) 'Update Kairo with a zip exits 0'
Check ((Get-StateObj).current -eq $v2) 'Update Kairo installed v2'

# ---- Start menu, auto-update task, updater folder
$menu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\Kairo'
foreach ($n in @('Kairo Logins', 'Kairo Check', 'Update Kairo')) {
    $lnk = Join-Path $menu "$n.lnk"
    $target = if (Test-Path $lnk) { (New-Object -ComObject WScript.Shell).CreateShortcut($lnk).TargetPath } else { '' }
    Check ($target -eq (Join-Path $Root "$n.bat") -and (Test-Path $target)) "Start menu -> Kairo -> $n opens $Root\$n.bat"
}
Check (-not (Test-Path (Join-Path $Root 'installer\bat'))) 'no confusing .bat copies in installer\bat'
$au = Get-ScheduledTask -TaskName 'Kairo Auto-Update' -TaskPath '\Kairo\'
Check ($au.Principal.UserId -eq 'SYSTEM' -or $au.Principal.UserId -eq 'S-1-5-18') 'auto-update task runs as SYSTEM'
Check ([string]$au.Triggers[0].Repetition.Interval -eq 'PT1M') 'auto-update checks every minute'
$updIds = @((Get-Acl (Join-Path $Root 'updater')).Access | ForEach-Object { $_.IdentityReference.Value })
Check (@($updIds | Where-Object { $_ -match 'kairo-svc|\\Users$|runneradmin' }).Count -eq 0) "updater folder: only Windows and admins ($($updIds -join ', '))"

# ---- auto-update: installs a new release by itself, notes it for the push, never retries a broken one
function Invoke-AutoUpdate([string]$Tag, [string]$Body, [string]$ZipFile) {
    $feed = Join-Path $work 'test-release.json'
    @{ tag_name = $Tag; body = $Body; zip = $ZipFile } | ConvertTo-Json | Set-Content -Path $feed -Encoding ASCII
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'installer\Kairo-AutoUpdate.ps1') -Root $Root -TestRelease $feed -CI | Out-Host
    return $LASTEXITCODE
}
$v4 = "$v1-ci4"
$s4 = Copy-Setup $s1 'v4' $v4
$zip4 = Join-Path $work "Kairo-Setup-$v4.zip"
Compress-Archive -Path $s4 -DestinationPath $zip4
# [urgent] so the test does not depend on the time of day (quiet hours).
Check ((Invoke-AutoUpdate "v$v4" '[urgent] CI' $zip4) -eq 0) 'auto-update exits 0'
Check ((Get-StateObj).current -eq $v4) "auto-update installed $v4"
$note = Get-Content -Raw (Join-Path $Root 'data\updates\last-update.json') | ConvertFrom-Json
Check ($note.message -eq "Kairo updated to $v4." -and -not $note.notified) 'one-line update note left for the iPhone push'
Check ((Invoke-AutoUpdate "v$v4" '[urgent] CI' $zip4) -eq 0 -and (Get-StateObj).current -eq $v4) 'already up to date: nothing happens'
$zip3 = Join-Path $work "Kairo-Setup-$v3.zip"
Compress-Archive -Path $s3 -DestinationPath $zip3
Check ((Invoke-AutoUpdate "v$v3" '[urgent] CI' $zip3) -ne 0) 'a broken release fails'
Check ((Get-StateObj).current -eq $v4) "still on $v4 after the broken auto-update"
$note = Get-Content -Raw (Join-Path $Root 'data\updates\last-update.json') | ConvertFrom-Json
Check ($note.message -match 'failed and was undone') 'the failure is noted for the iPhone push'
$logLines = @(Get-Content (Join-Path $Root 'updater\auto-update.log')).Count
Check ((Invoke-AutoUpdate "v$v3" '[urgent] CI' $zip3) -eq 0 -and @(Get-Content (Join-Path $Root 'updater\auto-update.log')).Count -eq $logLines) 'a failed version is not tried again'

# ---- uninstall keeps data
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'installer\Uninstall-Kairo.ps1') -CI -Root $Root | Out-Host
Check ($LASTEXITCODE -eq 0) 'uninstall exits 0'
Check (-not (Get-ScheduledTask -TaskName 'Kairo' -TaskPath '\Kairo\' -ErrorAction SilentlyContinue)) 'boot task removed'
Check (-not (Get-ScheduledTask -TaskName 'Kairo Boot Check' -TaskPath '\Kairo\' -ErrorAction SilentlyContinue)) 'boot check task removed'
Check (-not (Get-LocalUser -Name 'kairo-svc' -ErrorAction SilentlyContinue)) 'kairo-svc account removed'
Check (-not (Get-ScheduledTask -TaskName 'Kairo Sync' -TaskPath '\Kairo\' -ErrorAction SilentlyContinue)) 'sync task removed'
Check (-not (Get-NetFirewallRule -DisplayName 'Kairo Sync (Tailscale only)' -ErrorAction SilentlyContinue)) 'sync firewall rule removed'
Check (-not (Test-Path (Join-Path $Root 'app'))) 'program files removed'
Check (Test-Path (Join-Path $Root 'data\db\kairo.sqlite')) 'data kept'
Check ($null -eq (Get-HealthObj)) 'nothing listening any more'
Write-Host 'ALL INSTALLER CHECKS PASSED' -ForegroundColor Green
