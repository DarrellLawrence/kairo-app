# Vault sync with Syncthing (KICKOFF v5, Milestone M). PowerShell 5.1, ASCII-only.
#
# Syncthing runs as Kairo's own account (kairo-svc) and shares only
# C:\Kairo\data\vault. It listens only on the PC's Tailscale address (no
# public port), with discovery, relays and NAT traversal off: the laptop and
# the iPhone (Mobius Sync) reach it through the tailnet. Old versions of
# changed or deleted files are kept (staggered versioning, 30 days).

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Kairo.psm1') -Force -DisableNameChecking

$script:SyncTaskName = 'Kairo Sync'
$script:SyncFolderId = 'kairo-vault'
$script:SyncGui = 'http://127.0.0.1:8384'
$script:SyncFirewallRule = 'Kairo Sync (Tailscale only)'
$script:TailscaleExe2 = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'

function Get-SyncPaths($P) {
    return [pscustomobject]@{
        Exe  = Join-Path $P.Tools 'syncthing\syncthing.exe'
        Home = Join-Path $P.Data 'syncthing'
    }
}

function Get-TailscaleIPv4 {
    if (-not (Test-Path $script:TailscaleExe2)) { return $null }
    $r = Invoke-Native $script:TailscaleExe2 @('ip', '-4')
    $ip = (([string]$r.Out) -split "`r?`n" | Where-Object { $_ -match '^\s*100\.\d+\.\d+\.\d+\s*$' } | Select-Object -First 1)
    if ($r.Code -eq 0 -and $ip) { return $ip.Trim() }
    return $null
}

# The binary: from KAIRO_SYNCTHING (CI) or winget (it verifies the download).
# Copied into C:\Kairo\tools so Kairo's own account can run it.
function Install-Syncthing($P) {
    $S = Get-SyncPaths $P
    if (Test-Path $S.Exe) { Write-Ok 'Syncthing present'; return $S.Exe }
    $src = $env:KAIRO_SYNCTHING
    if (-not $src) {
        if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) { throw 'winget is missing, so Syncthing cannot be installed.' }
        Write-Note 'Installing Syncthing (vault sync)...'
        & winget.exe install --id Syncthing.Syncthing --exact --silent --scope machine --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Host
        if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) { throw "Installing Syncthing failed (winget $LASTEXITCODE)" }
        $roots = @("$env:ProgramFiles\WinGet\Packages", "$env:LOCALAPPDATA\Microsoft\WinGet\Packages") | Where-Object { Test-Path $_ }
        $found = $roots | ForEach-Object { Get-ChildItem -Path $_ -Recurse -Filter 'syncthing.exe' -ErrorAction SilentlyContinue } |
            Where-Object { $_.DirectoryName -match 'Syncthing' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $found) { throw 'Syncthing was installed but syncthing.exe was not found.' }
        $src = $found.FullName
    }
    [void](New-Item -ItemType Directory -Force (Split-Path $S.Exe))
    Copy-Item -Force $src $S.Exe
    Write-Ok ('Syncthing installed: ' + ([string](& $S.Exe --version 2>$null)).Trim())
    return $S.Exe
}

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $out = & $Exe @Arguments 2>&1 | Out-String } finally { $ErrorActionPreference = $old }
    return [pscustomobject]@{ Code = $LASTEXITCODE; Out = $out }
}

function Set-XmlChild($Doc, $Parent, [string]$Name, [string]$Value) {
    $node = $Parent.SelectSingleNode($Name)
    if (-not $node) { $node = $Doc.CreateElement($Name); [void]$Parent.AppendChild($node) }
    $node.InnerText = $Value
}

function Set-ListenAddresses($Doc, [string]$Ip) {
    $opts = $Doc.configuration.SelectSingleNode('options')
    foreach ($n in @($opts.SelectNodes('listenAddress'))) { [void]$opts.RemoveChild($n) }
    $host_ = if ($Ip) { $Ip } else { '127.0.0.1' }   # no Tailscale yet: nothing reachable from outside
    foreach ($a in @("tcp://${host_}:22000", "quic://${host_}:22000")) {
        $n = $Doc.CreateElement('listenAddress'); $n.InnerText = $a; [void]$opts.AppendChild($n)
    }
}

# First run: keys + config. Every run: enforce the safe options and the vault folder.
function Initialize-SyncConfig($P) {
    $S = Get-SyncPaths $P
    $cfg = Join-Path $S.Home 'config.xml'
    if (-not (Test-Path $cfg)) {
        [void](New-Item -ItemType Directory -Force $S.Home)
        $pw = New-RandomPassword
        $r = Invoke-Native $S.Exe @('generate', "--home=$($S.Home)", '--gui-user=kairo', "--gui-password=$pw", '--no-default-folder')
        if ($r.Code -ne 0 -or -not (Test-Path $cfg)) { throw "Syncthing could not create its settings: $($r.Out.Trim())" }
        Set-Content -Path (Join-Path $P.Secrets 'syncthing-gui.txt') -Value "user: kairo`r`npassword: $pw" -Encoding ASCII
    }
    [xml]$doc = Get-Content -Raw $cfg
    $c = $doc.configuration
    $gui = $c.SelectSingleNode('gui')
    Set-XmlChild $doc $gui 'address' '127.0.0.1:8384'
    $opts = $c.SelectSingleNode('options')
    foreach ($kv in @(@('globalAnnounceEnabled', 'false'), @('localAnnounceEnabled', 'false'), @('relaysEnabled', 'false'),
                      @('natEnabled', 'false'), @('urAccepted', '-1'), @('startBrowser', 'false'), @('crashReportingEnabled', 'false'),
                      @('autoUpgradeIntervalH', '0'))) {
        Set-XmlChild $doc $opts $kv[0] $kv[1]
    }
    Set-ListenAddresses $doc (Get-TailscaleIPv4)
    $folder = $c.SelectSingleNode("folder[@id='$script:SyncFolderId']")
    if (-not $folder) {
        $folder = $doc.CreateElement('folder')
        $folder.SetAttribute('id', $script:SyncFolderId)
        $folder.SetAttribute('label', 'Kairo vault')
        $folder.SetAttribute('type', 'sendreceive')
        $folder.SetAttribute('rescanIntervalS', '3600')
        $folder.SetAttribute('fsWatcherEnabled', 'true')
        $me = $doc.CreateElement('device')
        $me.SetAttribute('id', $c.SelectSingleNode('device').GetAttribute('id'))
        [void]$folder.AppendChild($me)
        $ver = $doc.CreateElement('versioning')
        $ver.SetAttribute('type', 'staggered')
        $p = $doc.CreateElement('param'); $p.SetAttribute('key', 'maxAge'); $p.SetAttribute('val', '2592000'); [void]$ver.AppendChild($p)
        [void]$folder.AppendChild($ver)
        [void]$c.InsertBefore($folder, $c.SelectSingleNode('device'))
    }
    $folder.SetAttribute('path', $P.Vault)
    $doc.Save($cfg)
}

function Register-SyncTask($P, $Svc) {
    $S = Get-SyncPaths $P
    $action = New-ScheduledTaskAction -Execute $S.Exe -Argument "serve --home=`"$($S.Home)`" --no-browser" -WorkingDirectory $S.Home
    $atBoot = New-ScheduledTaskTrigger -AtStartup
    $watchdog = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    [void](Register-ScheduledTask -TaskName $script:SyncTaskName -TaskPath $script:TaskPath -Action $action `
        -Trigger @($atBoot, $watchdog) -User $Svc.Name -Password $Svc.Password -RunLevel Limited -Settings $settings -Force `
        -Description 'Syncs the Kairo vault with your laptop and iPhone, only through Tailscale.')
    if (-not (Get-NetFirewallRule -DisplayName $script:SyncFirewallRule -ErrorAction SilentlyContinue)) {
        foreach ($proto in @('TCP', 'UDP')) {
            [void](New-NetFirewallRule -DisplayName $script:SyncFirewallRule -Direction Inbound -Action Allow -Program $S.Exe `
                -Protocol $proto -LocalPort 22000 -RemoteAddress '100.64.0.0/10')
        }
    }
}

function Stop-Sync {
    $t = Get-ScheduledTask -TaskName $script:SyncTaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue
    if ($t) { Stop-ScheduledTask -TaskName $script:SyncTaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process -Filter "Name = 'syncthing.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$($env:SystemDrive)\*Kairo*" } |
        ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }
}

function Start-Sync {
    Start-ScheduledTask -TaskName $script:SyncTaskName -TaskPath $script:TaskPath
}

function Unregister-Sync {
    Stop-Sync
    $t = Get-ScheduledTask -TaskName $script:SyncTaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue
    if ($t) { Unregister-ScheduledTask -TaskName $script:SyncTaskName -TaskPath $script:TaskPath -Confirm:$false }
    Get-NetFirewallRule -DisplayName $script:SyncFirewallRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}

# Install or update: binary, settings, task, firewall, (re)start.
function Install-Sync($P, $Svc) {
    Write-Step 'Vault sync (Syncthing, only through Tailscale)'
    [void](Install-Syncthing $P)
    Stop-Sync
    Initialize-SyncConfig $P
    Register-SyncTask $P $Svc
    Start-Sync
    Write-Ok 'Sync is running as Kairo''s account and shares only the vault folder'
}

function Get-SyncApiKey($P) {
    [xml]$doc = Get-Content -Raw (Join-Path (Get-SyncPaths $P).Home 'config.xml')
    return [string]$doc.configuration.gui.apikey
}

function Invoke-SyncApi($P, [string]$Method, [string]$Path, $Body = $null) {
    $h = @{ 'X-API-Key' = (Get-SyncApiKey $P) }
    $args_ = @{ Uri = "$script:SyncGui$Path"; Method = $Method; Headers = $h; UseBasicParsing = $true; TimeoutSec = 20 }
    if ($null -ne $Body) { $args_['Body'] = ($Body | ConvertTo-Json -Depth 6 -Compress); $args_['ContentType'] = 'application/json' }
    return Invoke-RestMethod @args_
}

function Wait-SyncApi($P, [int]$Seconds = 60) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try { return (Invoke-SyncApi $P 'GET' '/rest/system/status') } catch { Start-Sleep -Seconds 2 }
    }
    return $null
}

# Let a device (laptop, iPhone) in and share the vault with it.
function Add-SyncDevice($P, [string]$DeviceId, [string]$Name) {
    $id = $DeviceId.Trim().ToUpper()
    if ($id -notmatch '^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$') { throw 'That is not a Syncthing device ID (8 groups of 7 letters/digits).' }
    $known = @(Invoke-SyncApi $P 'GET' '/rest/config/devices' | ForEach-Object { $_.deviceID })
    if ($known -notcontains $id) {
        [void](Invoke-SyncApi $P 'POST' '/rest/config/devices' @{ deviceID = $id; name = $Name; addresses = @('dynamic'); autoAcceptFolders = $false })
    }
    $folder = Invoke-SyncApi $P 'GET' "/rest/config/folders/$script:SyncFolderId"
    if (@($folder.devices | ForEach-Object { $_.deviceID }) -notcontains $id) {
        $devs = @($folder.devices) + @(@{ deviceID = $id })
        [void](Invoke-SyncApi $P 'PATCH' "/rest/config/folders/$script:SyncFolderId" @{ devices = $devs })
    }
}

# Kairo Logins: make sure it listens on the Tailscale address, then walk the
# owner through the laptop and the iPhone.
function Connect-Sync($P, $Svc) {
    Write-Step 'Login 5 of 5: vault sync (laptop + iPhone)'
    $S = Get-SyncPaths $P
    if (-not (Test-Path $S.Exe)) {
        if (-not $Svc) { Write-Warn 'Sync is not installed yet. Run Update Kairo once, then Kairo Logins.'; return $false }
        Install-Sync $P $Svc
    }
    $ip = Get-TailscaleIPv4
    if (-not $ip) { Write-Warn 'Tailscale is not connected, so sync can not be set up yet. Finish the Tailscale step first.'; return $false }
    Stop-Sync; Initialize-SyncConfig $P; Start-Sync
    $st = Wait-SyncApi $P
    if (-not $st) { Write-Bad 'Syncthing did not start. Run Kairo Check.'; return $false }
    $tsName = ''
    try { $tsName = ([string]((& $script:TailscaleExe2 status --json | Out-String | ConvertFrom-Json).Self.DNSName)).TrimEnd('.') } catch { }
    $addr = if ($tsName) { "tcp://${tsName}:22000" } else { "tcp://${ip}:22000" }
    Write-Ok "Sync listens only on Tailscale ($ip)"
    Write-Host ''
    Write-Host "    This PC's device ID:" -ForegroundColor Cyan
    Write-Host "      $($st.myID)" -ForegroundColor Cyan
    Write-Host "    Its address:  $addr" -ForegroundColor Cyan
    Write-Note 'Laptop (once):'
    Write-Note '  1. Install Syncthing: in PowerShell run  winget install Syncthing.Syncthing  (or get SyncTrayzor).'
    Write-Note '  2. Open it (browser page http://127.0.0.1:8384) -> Add Remote Device -> paste the ID above.'
    Write-Note "     Advanced tab -> Addresses: $addr  -> Save."
    Write-Note '  3. Actions -> Show ID: copy the laptop''s own ID and paste it here.'
    Write-Note '  4. When "Kairo vault" is offered, accept it and pick the folder Obsidian should open.'
    $laptop = Read-Host '    Laptop device ID (or Enter to skip)'
    if ($laptop.Trim()) { try { Add-SyncDevice $P $laptop 'Laptop'; Write-Ok 'Laptop added; accept the folder on the laptop' } catch { Write-Bad $_.Exception.Message } }
    Write-Note 'iPhone (once): Tailscale app ON.'
    Write-Note '  1. App Store: install "Mobius Sync" (Syncthing for iPhone) and Obsidian.'
    Write-Note "  2. Mobius Sync -> Devices -> + -> paste the ID above; Addresses: $addr"
    Write-Note '  3. Mobius Sync -> This Device: copy its ID and paste it here.'
    Write-Note '  4. Accept "Kairo vault"; choose a folder inside Obsidian''s folder (On My iPhone -> Obsidian).'
    Write-Note '  5. Obsidian -> Open folder as vault -> that folder. Open Mobius Sync now and then to sync.'
    $phone = Read-Host '    iPhone device ID (or Enter to skip)'
    if ($phone.Trim()) { try { Add-SyncDevice $P $phone 'iPhone'; Write-Ok 'iPhone added; accept the folder in Mobius Sync' } catch { Write-Bad $_.Exception.Message } }
    return $true
}

Export-ModuleMember -Function * -Variable SyncTaskName, SyncFolderId, SyncFirewallRule
