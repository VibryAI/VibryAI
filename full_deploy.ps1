[CmdletBinding()]
param(
    [string]$RemoteHost = $(if ($env:VIBRY_REMOTE) { $env:VIBRY_REMOTE } else { "root@163.7.8.8" }),
    [string]$RemoteDir = $(if ($env:VIBRY_HOME) { $env:VIBRY_HOME } else { "/opt/http/vibryai/server" }),
    [string]$ServiceName = $(if ($env:VIBRY_SERVICE) { $env:VIBRY_SERVICE } else { "vibry-server" }),
    [int]$Port = $(if ($env:VIBRY_PORT) { [int]$env:VIBRY_PORT } else { 9999 }),
    [string]$Version = (Get-Date -Format "yyyyMMdd-HHmmss"),
    [switch]$SkipTests,
    [switch]$PackageOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

function Assert-SafeValue([string]$Name, [string]$Value, [string]$Pattern) {
    if ($Value -notmatch $Pattern) {
        throw "Unsafe $Name value: $Value"
    }
}

Assert-Command "tar"
Assert-SafeValue "Version" $Version '^[A-Za-z0-9._-]+$'
Assert-SafeValue "RemoteDir" $RemoteDir '^/[A-Za-z0-9._/-]+$'
Assert-SafeValue "ServiceName" $ServiceName '^[A-Za-z0-9._@-]+$'
if ($Port -lt 1 -or $Port -gt 65535) { throw "Invalid port: $Port" }

$Root = $PSScriptRoot
$ReleaseDir = Join-Path $Root "release"
$PackageName = "vibry-server-$Version"
$PackagePath = Join-Path $ReleaseDir "$PackageName.tar.gz"
$ChecksumPath = "$PackagePath.sha256"
$Python = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

if (-not (Test-Path (Join-Path $Root "run.py"))) {
    throw "Run this script from the VibryServer repository."
}

if (-not $SkipTests) {
    Write-Step "Running server tests"
    Push-Location $Root
    try {
        & $Python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "Tests failed; deployment package was not created." }
    }
    finally {
        Pop-Location
    }
}

Write-Step "Building full release package $PackageName"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
if (Test-Path $PackagePath) { Remove-Item -LiteralPath $PackagePath -Force }

$TarArgs = @(
    "-czf", $PackagePath,
    "--exclude=.git",
    "--exclude=.claude",
    "--exclude=.zcode",
    "--exclude=.hypothesis",
    "--exclude=.pytest_cache",
    "--exclude=venv",
    "--exclude=__pycache__",
    "--exclude=*.pyc",
    "--exclude=data",
    "--exclude=release",
    "--exclude=.env",
    "--exclude=*.db",
    "--exclude=*.db-shm",
    "--exclude=*.db-wal",
    "--exclude=*.log",
    "--exclude=*.zip",
    "--exclude=ffmpeg-win-*",
    "--exclude=nssm.*",
    "--exclude=*.bat",
    "."
)

Push-Location $Root
try {
    & tar @TarArgs
    if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $PackagePath).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText(
    $ChecksumPath,
    "$Hash  $PackageName.tar.gz`n",
    [System.Text.UTF8Encoding]::new($false)
)
$SizeMb = [math]::Round((Get-Item $PackagePath).Length / 1MB, 2)
Write-Host "Package:  $PackagePath"
Write-Host "SHA256:   $Hash"
Write-Host "Size:     $SizeMb MB"

if ($PackageOnly) {
    Write-Host "`nPackage-only mode complete." -ForegroundColor Green
    exit 0
}

Assert-Command "ssh"
Assert-Command "scp"
Assert-SafeValue "RemoteHost" $RemoteHost '^[A-Za-z0-9._@:-]+$'

$RemoteArchive = "/tmp/$PackageName.tar.gz"
$RemoteChecksum = "$RemoteArchive.sha256"
$RemoteStage = "/tmp/$PackageName"

Write-Step "Uploading package to $RemoteHost"
& scp $PackagePath "${RemoteHost}:$RemoteArchive"
if ($LASTEXITCODE -ne 0) { throw "Package upload failed." }
& scp $ChecksumPath "${RemoteHost}:$RemoteChecksum"
if ($LASTEXITCODE -ne 0) { throw "Checksum upload failed." }

Write-Step "Installing full release on the server"
$RemoteCommand = @"
set -e
cd /tmp
sha256sum -c '$RemoteChecksum'
rm -rf '$RemoteStage'
mkdir -p '$RemoteStage'
tar -xzf '$RemoteArchive' -C '$RemoteStage'
cd '$RemoteStage'
if sudo test -f '$RemoteDir/run.py'; then
  sudo env VIBRY_HOME='$RemoteDir' VIBRY_SERVICE='$ServiceName' VIBRY_PORT='$Port' bash deploy.sh --update
else
  sudo env VIBRY_HOME='$RemoteDir' VIBRY_SERVICE='$ServiceName' VIBRY_PORT='$Port' bash deploy.sh
fi
rm -rf '$RemoteStage' '$RemoteArchive' '$RemoteChecksum'
"@

& ssh $RemoteHost $RemoteCommand
if ($LASTEXITCODE -ne 0) { throw "Remote deployment failed. Check the server output above." }

Write-Step "Verifying the deployed service"
& ssh $RemoteHost "curl -fsS --max-time 10 http://127.0.0.1:$Port/api/health && echo"
if ($LASTEXITCODE -ne 0) { throw "Final remote health check failed." }

Write-Host "`nFull deployment completed successfully." -ForegroundColor Green
Write-Host "Dashboard: http://163.7.8.8/admin"
