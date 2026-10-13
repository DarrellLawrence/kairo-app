# "Update Kairo": download the newest tested release from GitHub (or use a zip
# you give it), verify its checksum, and install it with the installer that
# comes inside it (backup, switch, health check, automatic rollback).
param(
    [string]$Root = 'C:\Kairo',
    [string]$ZipPath = '',
    [switch]$CI
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Kairo.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $PSScriptRoot 'KairoSetup.psm1') -Force -DisableNameChecking

$passArgs = @('-Root', "`"$Root`"")
if ($ZipPath) { $passArgs += @('-ZipPath', "`"$ZipPath`"") }
if ($CI) { $passArgs += '-CI' }
Assert-Admin $PSCommandPath $passArgs

$P = Get-KairoPaths $Root
$exitCode = 0

function Get-Download([string]$Url, [string]$Token, [string]$OutFile) {
    # GitHub answers asset downloads with a redirect to a signed URL. Follow it
    # by hand so the token is never sent to the other host.
    $req = [Net.HttpWebRequest]::Create($Url)
    $req.AllowAutoRedirect = $false
    $req.UserAgent = 'kairo-updater'
    $req.Accept = 'application/octet-stream'
    $req.Headers.Add('Authorization', "Bearer $Token")
    $resp = $null
    try { $resp = $req.GetResponse() } catch [Net.WebException] { $resp = $_.Exception.Response }
    if (-not $resp) { throw "Download failed: $Url" }
    $status = [int]$resp.StatusCode
    $location = $resp.Headers['Location']
    $resp.Close()
    if ($status -ge 300 -and $status -lt 400 -and $location) {
        Invoke-WebRequest -Uri $location -OutFile $OutFile -UseBasicParsing -UserAgent 'kairo-updater'
    } elseif ($status -eq 200) {
        $h = @{ Authorization = "Bearer $Token"; Accept = 'application/octet-stream'; 'User-Agent' = 'kairo-updater' }
        Invoke-WebRequest -Uri $Url -Headers $h -OutFile $OutFile -UseBasicParsing
    } else { throw "Download failed with HTTP $status" }
}

try {
    $state = Read-State $P
    if (-not $state.current) { throw 'Kairo is not installed yet. Use "Install Kairo.bat" from the setup zip first.' }
    Write-Step "Kairo update (installed: $($state.current))"
    Initialize-Folders $P

    if (-not $ZipPath) {
        $repo = [string](Read-ReleaseInfo $P.Current).update_repo
        $token = Get-GitHubToken $P
        if (-not $token -or -not (Test-GitHubToken $token $repo)) {
            Write-Warn 'Update access is missing or expired.'
            if (-not (Connect-GitHub $P $repo)) { throw 'No update access. Nothing was changed.' }
            $token = Get-GitHubToken $P
        }
        $h = @{ Authorization = "Bearer $token"; Accept = 'application/vnd.github+json'; 'User-Agent' = 'kairo-updater' }
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $h -UseBasicParsing
        $latest = ([string]$rel.tag_name) -replace '^v', ''
        if ($latest -eq $state.current) {
            Write-Ok "Kairo is up to date ($latest)."
            throw (New-Object System.OperationCanceledException 'up to date')
        }
        Write-Note "Newest tested version: $latest"
        $zipAsset = @($rel.assets | Where-Object { $_.name -like 'Kairo-Setup-*.zip' }) | Select-Object -First 1
        $shaAsset = @($rel.assets | Where-Object { $_.name -like 'Kairo-Setup-*.zip.sha256' }) | Select-Object -First 1
        if (-not $zipAsset -or -not $shaAsset) { throw 'The release has no setup zip or checksum. Nothing was changed.' }
        $ZipPath = Join-Path $P.Downloads $zipAsset.name
        $shaPath = "$ZipPath.sha256"
        Write-Note 'Downloading...'
        Get-Download $zipAsset.url $token $ZipPath
        Get-Download $shaAsset.url $token $shaPath
        $expected = ((Get-Content -Raw $shaPath).Trim() -split '\s+')[0].ToLower()
        $actual = (Get-FileHash -Algorithm SHA256 $ZipPath).Hash.ToLower()
        if ($expected -ne $actual) { throw 'Checksum mismatch: the download is damaged or not genuine. Nothing was changed.' }
        Write-Ok 'Download verified (SHA-256)'
    }

    $extract = Join-Path $P.Downloads ([IO.Path]::GetFileNameWithoutExtension($ZipPath))
    if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }
    Expand-Archive -Path $ZipPath -DestinationPath $extract
    $inner = Get-ChildItem $extract -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'installer\Install-Kairo.ps1') } | Select-Object -First 1
    $setupDir = if ($inner) { $inner.FullName } else { $extract }
    $installArgs = @{ Root = $Root; Update = $true }
    if ($CI) { $installArgs['CI'] = $true; $installArgs['ClaudeBin'] = (Read-EnvFile $P)['KAIRO_CLAUDE_BIN'] }
    & (Join-Path $setupDir 'installer\Install-Kairo.ps1') @installArgs
    $exitCode = $LASTEXITCODE
} catch [System.OperationCanceledException] {
    $exitCode = 0
} catch {
    Write-Bad $_.Exception.Message
    $exitCode = 1
}
if (-not $CI -and $exitCode -eq 0) { Wait-Enter 'Press Enter to close this window' }
exit $exitCode
