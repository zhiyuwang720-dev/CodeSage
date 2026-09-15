param(
    [ValidateSet("foundation", "packets", "graph", "all")]
    [string]$Suite = "foundation"
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$tempRoot = Join-Path $root ".tmp\plan21-acceptance"
New-Item -ItemType Directory -Force -Path (Split-Path $tempRoot -Parent) | Out-Null

if ($Suite -eq "graph") {
    Write-Error "Plan 21M is not approved or implemented."
    exit 2
}
if ($Suite -eq "packets") {
    Write-Error "Plan 21B is not implemented yet."
    exit 2
}

Push-Location $root
try {
    & python -m pytest tests/review_context_acceptance -q -p no:cacheprovider --basetemp $tempRoot
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
