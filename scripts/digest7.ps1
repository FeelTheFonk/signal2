[CmdletBinding()]
param(
    [switch]$Publish,
    [switch]$Cloud
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

if ($Cloud) {
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $gh) {
        throw 'GitHub CLI (gh) is required only for -Cloud. You can also run the workflow manually from the GitHub Actions UI.'
    }

    $publishValue = if ($Publish) { 'true' } else { 'false' }
    Push-Location $repoRoot
    try {
        & gh workflow view signal2.yml *> $null
        if ($LASTEXITCODE -ne 0) {
            throw 'signal2.yml is not deployed on GitHub main. Commit/push the new .github/workflows/signal2.yml first, then retry -Cloud.'
        }

        & gh workflow run signal2.yml -f mode=digest -f days=7 -f publish=$publishValue
        if ($LASTEXITCODE -ne 0) { throw "gh workflow run failed with exit code $LASTEXITCODE" }
        Write-Host "Signal2 digest workflow dispatched (7 days, publish=$publishValue)."
    }
    finally {
        Pop-Location
    }
    exit 0
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $python) {
    throw 'Python 3 is required for a local dry-run. Use -Cloud to execute on GitHub Actions instead.'
}

Push-Location $repoRoot
try {
    $argsList = @('signal2.py', '--digest', '7')
    if ($Publish) {
        if (-not $env:SIGNAL2_WEBHOOK) {
            throw 'SIGNAL2_WEBHOOK must be set for local publishing.'
        }
        $argsList += '--publish'
    }

    if ($python.Name -eq 'py.exe' -or $python.Name -eq 'py') {
        & $python.Source -3 @argsList
    }
    else {
        & $python.Source @argsList
    }
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
