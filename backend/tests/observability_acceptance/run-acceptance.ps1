param(
    [ValidateSet("model-foundation", "unit", "integration", "all")]
    [string]$Suite = "all",
    [switch]$KeepDependencies
)

$ErrorActionPreference = "Stop"

# Variables must be assigned before the helper functions are defined: PowerShell 5.1
# binds script-scope variable names at definition time.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = (Resolve-Path (Join-Path $here "..\..")).Path
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$commit = (git -C $backend rev-parse --short HEAD).Trim()
$artifactRoot = Join-Path $backend ".acceptance-artifacts\plan20p0\$stamp-$commit"
$pytestTemp = Join-Path $artifactRoot "pytest"
New-Item -ItemType Directory -Force -Path $artifactRoot, $pytestTemp | Out-Null

$env:CODESAGE_ACCEPTANCE_ARTIFACT_ROOT = Join-Path $backend ".acceptance-artifacts\plan20p0"
$env:CODESAGE_ACCEPTANCE_STAMP = $stamp
$env:CODESAGE_ACCEPTANCE_COMMIT = $commit
# The in-process fixture must be reached directly: developer machines often set an
# HTTP proxy that refuses random local ports.
$env:NO_PROXY = "127.0.0.1,localhost"
$env:no_proxy = "127.0.0.1,localhost"

$modelFoundationTests = @(
    "tests/observability_acceptance/test_ap01_model_boundary_static.py",
    "tests/observability_acceptance/test_ap02_wire_semantics.py",
    "tests/observability_acceptance/test_ap03_sdk_callbacks.py",
    "tests/observability_acceptance/test_ap04_retry_budget.py",
    "tests/observability_acceptance/test_ap05_usage_and_cost.py",
    "tests/observability_acceptance/test_ap06_concurrency_isolation.py",
    "tests/observability_acceptance/test_ap07_ap08_stream_contract.py",
    "tests/observability_acceptance/test_ap09_ap10_cancellation.py",
    "tests/observability_acceptance/test_ap12_resume_fingerprint.py",
    "tests/observability_acceptance/test_ap14_observability_cost.py",
    "tests/observability_acceptance/test_ap15_deletion_and_isolation.py",
    "tests/observability_acceptance/test_usage_normalization.py",
    "tests/observability_acceptance/test_model_identity_flow.py",
    "tests/observability_acceptance/test_model_boundary_evidence.py",
    "tests/observability_acceptance/test_capability_matrix.py"
)

$regressionTests = @(
    "tests/architecture",
    "tests/observability",
    "tests/runtime",
    "tests/session",
    "tests/pr_review",
    "tests/services",
    "tests/tooling",
    "tests/hooks",
    "tests/memory",
    "tests/permission",
    "tests/skill",
    "tests/agent/test_llm_service.py",
    "tests/agent/test_model_config.py"
)

function Write-EvidenceNote([string]$RelativePath, [AllowEmptyString()][string]$Content) {
    $full = $artifactRoot + "\" + $RelativePath
    $parent = Split-Path -Parent $full
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    if ($null -eq $Content) { $Content = "" }
    Set-Content -LiteralPath $full -Value $Content -Encoding UTF8
}

function ConvertTo-JsonSafe($Value) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $json = ConvertTo-Json -InputObject $Value -Depth 5
    }
    catch {
        $json = "{}"
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($null -eq $json) { return "{}" }
    return [string]$json
}

function Get-PythonProbe([string]$Expression) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $value = (@(& python -c $Expression) -join " ").Trim()
    }
    catch {
        $value = ""
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ([string]::IsNullOrWhiteSpace($value)) { return "unknown" }
    return $value
}

function Invoke-PytestStep([string]$StepName, [string[]]$StepArguments) {
    $stdout = Join-Path $artifactRoot "$StepName.stdout.log"
    $junit = Join-Path $artifactRoot "$StepName.junit.xml"
    # Separate basetemp per step: the previous step's temp dir may still be held open.
    $stepTemp = Join-Path $artifactRoot "pytest-$StepName"
    New-Item -ItemType Directory -Force -Path $stepTemp | Out-Null
    $pyArgs = @("-m", "pytest") + $StepArguments + @(
        "--basetemp=$stepTemp",
        "-o", "cache_dir=$artifactRoot/.pytest-cache",
        "-p", "no:cacheprovider",
        "--junitxml=$junit"
    )
    # pytest writes warnings and tracebacks to stderr; while $ErrorActionPreference is
    # Stop a native command's stderr becomes a terminating error, so relax it here and
    # rely on $LASTEXITCODE only.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & python @pyArgs *> $stdout
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    Write-Host "[$StepName] exit=$code -> $stdout"
    return $code
}

Push-Location $backend
try {
    $gitStatus = @(git -C $backend status --porcelain)
    if ($env:CODESAGE_ACCEPTANCE_POSTGRES_URL) {
        $postgresState = "provided"
    }
    else {
        $postgresState = "absent"
    }
    $manifest = [ordered]@{
        suite          = $Suite
        utc_stamp      = $stamp
        commit         = (git -C $backend rev-parse HEAD).Trim()
        commit_short   = $commit
        dirty          = $gitStatus
        python         = Get-PythonProbe "import sys; print(sys.version.split()[0])"
        litellm        = Get-PythonProbe "from importlib.metadata import version; print(version('litellm'))"
        openai         = Get-PythonProbe "from importlib.metadata import version; print(version('openai'))"
        anthropic      = Get-PythonProbe "from importlib.metadata import version; print(version('anthropic'))"
        httpx          = Get-PythonProbe "from importlib.metadata import version; print(version('httpx'))"
        opentelemetry  = Get-PythonProbe "from importlib.metadata import version; print(version('opentelemetry-sdk'))"
        postgres_url   = $postgresState
        paid_endpoints = "not used: all acceptance runs hit the in-process HTTP/SSE fixture"
    }
    Write-EvidenceNote "manifest.json" (ConvertTo-JsonSafe $manifest)
    Write-EvidenceNote "baseline.txt" (((& git -C $backend log --oneline -5 | Out-String) + "`n" + ($gitStatus -join "`n")))
    # Capability/config/retry tables are repo-reviewed artifacts; copy them next to the
    # run output so a single directory answers "what was claimed, with what evidence".
    foreach ($name in @("capability-matrix.json", "config-mapping.json", "retry-budget.json")) {
        Copy-Item -LiteralPath (Join-Path $backend "tests\observability_acceptance\$name") -Destination (Join-Path $artifactRoot $name) -Force
    }
    Copy-Item -LiteralPath (Join-Path $backend "tests\observability_acceptance\model_harness.py") -Destination (Join-Path $artifactRoot "fixture-harness.py") -Force

    $exitCode = 0

    if ($Suite -in @("model-foundation", "all")) {
        $modelArgs = $modelFoundationTests + @("-q")
        $code = Invoke-PytestStep "model-foundation" $modelArgs
        if ($code -ne 0) { $exitCode = 1 }
        $regressionArgs = $regressionTests + @("-q")
        $code = Invoke-PytestStep "regression" $regressionArgs
        if ($code -ne 0) { $exitCode = 1 }
    }

    if ($Suite -eq "unit") {
        $code = Invoke-PytestStep "unit" @(
            "tests/observability_acceptance",
            "tests/observability",
            "tests/runtime",
            "tests/agent/test_llm_service.py",
            "-q"
        )
        if ($code -ne 0) { $exitCode = 1 }
    }

    if ($Suite -in @("integration", "all")) {
        $workerAcceptance = Join-Path $backend "tests\worker_acceptance\run-acceptance.ps1"
        & $workerAcceptance -KeepDependencies:$KeepDependencies
        if ($LASTEXITCODE -ne 0) { $exitCode = 2 }
    }

    if ($exitCode -eq 0) {
        # Missing evidence must not exit 0: exit code 2 means environment/evidence gap.
        $required = @(
            "manifest.json",
            "model-foundation.junit.xml",
            "regression.junit.xml",
            "capability-matrix.json",
            "config-mapping.json",
            "retry-budget.json",
            "evidence/ap01/callers.json",
            "evidence/ap03/callback_granularity.json",
            "evidence/ap04/retry_budget.json",
            "evidence/ap05/price_boundary.json",
            "evidence/ap13/provider_span.json",
            "evidence/ap14/cost_snapshot.json",
            "evidence/ap15/deletion-map.json"
        )
        foreach ($item in $required) {
            if (-not (Test-Path (Join-Path $artifactRoot $item))) {
                Write-Warning "missing evidence: $item"
                $exitCode = 2
            }
        }
    }

    Write-EvidenceNote "result.txt" "exit_code=$exitCode`nsuite=$Suite`nartifacts=$artifactRoot"
    Write-Host "artifacts: $artifactRoot"
    exit $exitCode
}
catch {
    Write-Error $_
    exit 2
}
finally {
    Pop-Location
}
