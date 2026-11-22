$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoZipUrl = "https://github.com/cirrus2026-dev/vertex-studio-oss/archive/refs/heads/main.zip"
$PreserveNames = @(".bridge_token", ".bridge_token.txt", "argon_history", "carbon_history", "config.json", ".env", ".git")

Set-Location $RepoDir

if ((Test-Path (Join-Path $RepoDir ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "[Updater] Git repository detected. Running git pull --ff-only..." -ForegroundColor Cyan
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        throw "git pull failed (exit code $LASTEXITCODE). Resolve local changes or stash them first."
    }
} else {
    Write-Host "[Updater] No .git folder found. Downloading latest release ZIP from GitHub..." -ForegroundColor Cyan
    $TempRoot = Join-Path $env:TEMP ("vertex_studio_update_" + [guid]::NewGuid().ToString("N"))
    $ZipPath = Join-Path $TempRoot "main.zip"
    $ExtractDir = Join-Path $TempRoot "extracted"

    try {
        New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
        Invoke-WebRequest -Uri $RepoZipUrl -OutFile $ZipPath -UseBasicParsing
        Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force

        $SourceRoot = Get-ChildItem -Path $ExtractDir -Directory | Select-Object -First 1
        if (-not $SourceRoot) {
            throw "Extracted ZIP is empty."
        }

        Write-Host "[Updater] Updating files (preserving tokens, history, and local configs)..." -ForegroundColor Cyan
        Get-ChildItem -Path $SourceRoot.FullName -Force | ForEach-Object {
            if ($PreserveNames -contains $_.Name) {
                return
            }
            $DestPath = Join-Path $RepoDir $_.Name
            if ($_.PSIsContainer) {
                Copy-Item -Path $_.FullName -Destination $RepoDir -Recurse -Force
            } else {
                Copy-Item -Path $_.FullName -Destination $DestPath -Force
            }
        }
    } finally {
        if (Test-Path $TempRoot) {
            Remove-Item -Path $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    Write-Host "[Updater] Verifying Python syntax..." -ForegroundColor Cyan
    python -m py_compile (Join-Path $RepoDir "chrome_cdp.py") (Join-Path $RepoDir "bundle_project.py") (Join-Path $RepoDir "gemini_bridge_server.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Python syntax check failed."
    }
}

Write-Host "[Updater] All files are up to date." -ForegroundColor Green
