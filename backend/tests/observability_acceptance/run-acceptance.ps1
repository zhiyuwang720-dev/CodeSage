param(
    [ValidateSet("unit", "integration", "all")]
    [string]$Suite = "all",
    [switch]$KeepDependencies
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Resolve-Path (Join-Path $here "..\..")
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$commit = (git -C $backend rev-parse --short HEAD).Trim()
$artifactRoot = Join-Path $backend ".acceptance-artifacts\plan20\$stamp-$commit"
$pytestTemp = Join-Path $artifactRoot "pytest"
New-Item -ItemType Directory -Force -Path $artifactRoot, $pytestTemp | Out-Null

function Invoke-Pytest([string[]]$Arguments) {
    & python -m pytest @Arguments "--basetemp=$pytestTemp" "-o" "cache_dir=$artifactRoot/.pytest-cache"
    if ($LASTEXITCODE -ne 0) {
        exit 1
    }
}

Push-Location $backend
try {
    if ($Suite -in @("unit", "all")) {
        Invoke-Pytest @(
            "tests/observability_acceptance",
            "tests/observability",
            "tests/agent/test_llm_service.py",
            "tests/agent/test_litellm_adapter.py",
            "tests/agent/test_anthropic_adapter.py",
            "tests/runtime/test_bridge.py",
            "tests/runtime/test_query_loop.py",
            "tests/runtime/test_compact_runtime.py",
            "-q"
        )
    }

    if ($Suite -in @("integration", "all")) {
        $workerAcceptance = Join-Path $backend "tests\worker_acceptance\run-acceptance.ps1"
        & $workerAcceptance -KeepDependencies:$KeepDependencies
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
}
catch {
    Write-Error $_
    exit 2
}
finally {
    Pop-Location
}

exit 0
