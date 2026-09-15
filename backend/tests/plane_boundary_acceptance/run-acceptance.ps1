param(
    [ValidateSet("boundaries", "nodes", "remote", "all")]
    [string]$Suite = "boundaries",
    [int]$Repeat = 1
)

$ErrorActionPreference = "Stop"
$backend = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$tempRoot = Join-Path $backend ".tmp\plan22-boundaries"

if ($Suite -ne "boundaries") {
    Write-Error "Suite '$Suite' is not implemented until its ordered prerequisite is verified."
    exit 2
}
if ($Repeat -lt 1) {
    Write-Error "Repeat must be at least 1."
    exit 2
}

Push-Location $backend
try {
    & python -m pytest tests/plane_boundary_acceptance -q -p no:cacheprovider --basetemp $tempRoot
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
