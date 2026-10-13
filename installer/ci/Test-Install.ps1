# CI end-to-end test of the Windows installer on a GitHub Windows runner.
# Not shipped in the release zip. Runs as admin (the runner is admin).
param([Parameter(Mandatory)][string]$Zip, [Parameter(Mandatory)][string]$FakeClaude)

$ErrorActionPreference = 'Stop'
$Root = 'C:\KairoCI'
$work = Join-Path $env:RUNNER_TEMP 'kairo-ci'
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
[void](New-Item -ItemType Directory $work)

function Check([bool]$Cond, [string]$Msg) {
    if (-not $Cond) { Write-Host "FAIL: $Msg" -ForegroundColor Red; throw "FAIL: $Msg" }
    Write-Host "PASS: $Msg" -ForegroundColor Green
}
function Expand-Setup([string]$ZipFile, [string]$Name) {
    $dest = Join-Path $work $Name
    Expand-Archive -Path $ZipFile -DestinationPath $dest -Force
    return (Get-ChildItem $dest -Directory | Select-Object -First 1).FullName
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

# ---- first install
$s1 = Expand-Setup $Zip 'v1'
$v1 = (Get-Content -Raw (Join-Path $s1 'app\release.json') | ConvertFrom-Json).version
Check ((Invoke-Install $s1) -eq 0) 'first install exits 0'
Check ((Get-StateObj).current -eq $v1) "running version is $v1"
$h = Get-HealthObj
Check ($h -ne $null -and $h.status -eq 'ok') 'health: ok'
Check ($h.brain -eq 'ok') 'health: brain ok (fake CLI found)'
$task = Get-ScheduledTask -TaskName 'Kairo' -TaskPath '\Kairo\'
Check ($task.Principal.LogonType -eq 'S4U') 'boot task runs without a stored password (S4U)'
Check ($task.Principal.RunLevel -eq 'Limited') 'boot task runs without admin rights'
Check (@($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' }).Count -eq 1) 'boot trigger present'
Check ($task.Settings.ExecutionTimeLimit -eq 'PT0S') 'no time limit on the task'
Check ($task.State -eq 'Running') 'task is running'
$listen = @(Get-NetTCPConnection -LocalPort 8000 -State Listen)
Check ($listen.Count -ge 1 -and @($listen | Where-Object { $_.LocalAddress -ne '127.0.0.1' }).Count -eq 0) 'listens on 127.0.0.1 only'
$ids = @((Get-Acl (Join-Path $Root 'data')).Access | ForEach-Object { $_.IdentityReference.Value })
Check (@($ids | Where-Object { $_ -match '\\Users$|^Everyone$|Authenticated Users' }).Count -eq 0) 'data folder: no access for other users'
$codeAcl = (Get-Acl (Join-Path $Root 'app')).Access | Where-Object { $_.IdentityReference.Value -eq [Security.Principal.WindowsIdentity]::GetCurrent().Name }
Check (@($codeAcl | Where-Object { $_.FileSystemRights -match 'Write|Modify|FullControl' }).Count -eq 0) 'code folder: owner cannot modify (read/run only)'
Check (Test-Path (Join-Path $Root 'Update Kairo.bat')) 'Update/Rollback/Uninstall .bat files placed in the Kairo folder'
Check ((Invoke-Install $s1) -eq 0) 'running the same version again is harmless'

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
Check (-not (Test-Path (Join-Path $Root 'app'))) 'program files removed'
Check (Test-Path (Join-Path $Root 'data\db\kairo.sqlite')) 'data kept'
Check ($null -eq (Get-HealthObj)) 'nothing listening any more'
Write-Host 'ALL INSTALLER CHECKS PASSED' -ForegroundColor Green
