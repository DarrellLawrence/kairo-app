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
        $p = Start-Process -FilePath $env:ComSpec -ArgumentList "/d /c `"`"$Bat`" $BatArgs`"" -NoNewWindow -Wait -PassThru `
            -RedirectStandardInput $in -RedirectStandardOutput $out -RedirectStandardError $err
    } finally {
        Remove-Item Env:\KAIRO_ROOT
        if ($savedPolicy) { $env:PSExecutionPolicyPreference = $savedPolicy }
    }
    $text = ((Get-Content -Raw $out -ErrorAction SilentlyContinue) + (Get-Content -Raw $err -ErrorAction SilentlyContinue))
    Write-Host $text
    return [pscustomobject]@{ Code = $p.ExitCode; Output = [string]$text }
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

# ---- the boot check (what runs after every reboot), minus the Tailscale parts
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'installer\Kairo-BootCheck.ps1') -Root $Root -CI | Out-Host
Check ($LASTEXITCODE -eq 0) 'boot check passes (Kairo up, own account, Claude answers)'
$bc = Get-Content -Raw (Join-Path $Root 'data\logs\boot-check.json') | ConvertFrom-Json
Check ($bc.all_ok -and @($bc.checks).Count -eq 3) 'boot-check.json written with 3 green checks'
$status = (Invoke-WebRequest 'http://127.0.0.1:8000/api/status' -UseBasicParsing).Content | ConvertFrom-Json
Check ($status.boot_check.all_ok) 'the app shows the boot check result (More screen)'

# ---- update keeps owner config
Add-Content (Join-Path $Root 'data\config\profile.yaml') '# ci-marker'
$v2 = "$v1-ci2"
$s2 = Copy-Setup $s1 'v2' $v2
Check ((Invoke-Install $s2) -eq 0) 'update to v2 exits 0'
$st = Get-StateObj
Check ($st.current -eq $v2 -and $st.previous -eq $v1) 'v2 running, v1 kept for rollback'
Check ((Get-Content -Raw (Join-Path $Root 'data\config\profile.yaml')) -match 'ci-marker') 'owner config untouched by the update'

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

# ---- uninstall keeps data
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'installer\Uninstall-Kairo.ps1') -CI -Root $Root | Out-Host
Check ($LASTEXITCODE -eq 0) 'uninstall exits 0'
Check (-not (Get-ScheduledTask -TaskName 'Kairo' -TaskPath '\Kairo\' -ErrorAction SilentlyContinue)) 'boot task removed'
Check (-not (Get-ScheduledTask -TaskName 'Kairo Boot Check' -TaskPath '\Kairo\' -ErrorAction SilentlyContinue)) 'boot check task removed'
Check (-not (Get-LocalUser -Name 'kairo-svc' -ErrorAction SilentlyContinue)) 'kairo-svc account removed'
Check (-not (Test-Path (Join-Path $Root 'app'))) 'program files removed'
Check (Test-Path (Join-Path $Root 'data\db\kairo.sqlite')) 'data kept'
Check ($null -eq (Get-HealthObj)) 'nothing listening any more'
Write-Host 'ALL INSTALLER CHECKS PASSED' -ForegroundColor Green
