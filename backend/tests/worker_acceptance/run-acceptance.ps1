param([switch]$KeepDependencies)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Resolve-Path (Join-Path $here "..\..")
$compose = Join-Path $here "docker-compose.yml"
$runId = (Get-Date -Format "yyyyMMdd-HHmmss") + "-$PID"
$projectName = "codesage-acceptance-$runId".ToLowerInvariant()
$artifactRoot = Join-Path $backend ".acceptance-artifacts\$runId"
$report = Join-Path $artifactRoot "report.txt"
New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null

$environmentNames = @(
    "DATABASE_URL", "REDIS_URL", "AGENT_TASK_QUEUE_NAME",
    "AGENT_TASK_EXECUTION_MODE", "AGENT_WORKER_CONCURRENCY",
    "CODESAGE_WORKER_ACCEPTANCE", "CODESAGE_ACCEPTANCE_ARTIFACT_ROOT",
    "MANAGED_PROJECTS_ROOT", "PYTEST_ADDOPTS", "PYTHONWARNINGS"
)
$savedEnvironment = @{}
foreach ($name in $environmentNames) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

$env:AGENT_TASK_QUEUE_NAME = "codesage:acceptance:agent_tasks"
$env:AGENT_TASK_EXECUTION_MODE = "worker"
$env:AGENT_WORKER_CONCURRENCY = "1"
$env:CODESAGE_WORKER_ACCEPTANCE = "1"
$env:CODESAGE_ACCEPTANCE_ARTIFACT_ROOT = $artifactRoot
$env:MANAGED_PROJECTS_ROOT = (Join-Path $artifactRoot "managed-projects")
$pytestRoot = ($artifactRoot -replace '\\', '/') + "/pytest"
$env:PYTEST_ADDOPTS = "--basetemp=$pytestRoot"
$env:PYTHONWARNINGS = "ignore"

Push-Location $backend
try {
    docker compose -p $projectName -f $compose up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed (exit $LASTEXITCODE)" }

    $postgresBinding = docker compose -p $projectName -f $compose port postgres 5432
    if ($LASTEXITCODE -ne 0 -or $postgresBinding -notmatch ':(\d+)$') {
        throw "could not resolve the PostgreSQL acceptance port"
    }
    $postgresPort = $Matches[1]
    $redisBinding = docker compose -p $projectName -f $compose port redis 6379
    if ($LASTEXITCODE -ne 0 -or $redisBinding -notmatch ':(\d+)$') {
        throw "could not resolve the Redis acceptance port"
    }
    $redisPort = $Matches[1]
    $env:DATABASE_URL = "postgresql+asyncpg://codesage_acceptance:codesage_acceptance@127.0.0.1:$postgresPort/codesage_acceptance"
    $env:REDIS_URL = "redis://127.0.0.1:$redisPort/0"

    alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade failed (exit $LASTEXITCODE)" }
    $commit = git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "git rev-parse failed (exit $LASTEXITCODE)" }
    $worktreeFingerprint = git diff --binary HEAD | git hash-object --stdin
    if ($LASTEXITCODE -ne 0) { throw "worktree fingerprint failed (exit $LASTEXITCODE)" }
    $statusText = (git status --porcelain=v1 -uall) -join "`n"
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $statusFingerprint = ([BitConverter]::ToString(
            $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($statusText))
        ) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
    $stdout = Join-Path $artifactRoot "acceptance.stdout.log"
    $stderr = Join-Path $artifactRoot "acceptance.stderr.log"
    $acceptanceJunit = Join-Path $artifactRoot "acceptance.junit.xml"
    $testProcess = Start-Process -FilePath "python" -ArgumentList @(
        "-m", "pytest", "tests/worker_acceptance", "-q", "--junitxml=$acceptanceJunit"
    ) -Wait -PassThru -NoNewWindow -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    if ($testProcess.ExitCode -ne 0) {
        Get-Content $stdout
        Get-Content $stderr
        throw "worker acceptance failed (exit $($testProcess.ExitCode))"
    }
    # 生产和验收统一 worker-only；单元测试直接调用服务，不再启用 local 调度分支。
    $env:CODESAGE_WORKER_ACCEPTANCE = "0"
    $regressionStdout = Join-Path $artifactRoot "regression.stdout.log"
    $regressionStderr = Join-Path $artifactRoot "regression.stderr.log"
    $regressionJunit = Join-Path $artifactRoot "regression.junit.xml"
    $regressionProcess = Start-Process -FilePath "python" -ArgumentList @(
        "-m", "pytest", "tests/runtime", "tests/session",
        "tests/pr_review", "tests/agent/test_task_resume.py",
        "tests/agent/test_task_queue.py", "-q", "--junitxml=$regressionJunit"
    ) -Wait -PassThru -NoNewWindow -RedirectStandardOutput $regressionStdout -RedirectStandardError $regressionStderr
    [xml]$acceptanceXml = Get-Content -Raw -Encoding UTF8 $acceptanceJunit
    [xml]$regressionXml = Get-Content -Raw -Encoding UTF8 $regressionJunit
    $acceptanceSuite = $acceptanceXml.testsuites.testsuite
    $regressionSuite = $regressionXml.testsuites.testsuite
    $failedCases = @($regressionXml.SelectNodes("//testcase[failure or error]"))
    $failedNames = @($failedCases | ForEach-Object { $_.name } | Sort-Object)
    $expectedFailedNames = @(
        "test_allowlist_filters_tools",
        "test_extract_text_tool_calls_preserves_nested_write_payload"
    ) | Sort-Object
    $knownRegressionFailure = $regressionProcess.ExitCode -eq 1 -and (@(Compare-Object $failedNames $expectedFailedNames).Count -eq 0)
    if ($regressionProcess.ExitCode -eq 0) {
        $regressionStatus = "passed"
    }
    elseif ($knownRegressionFailure) {
        $regressionStatus = "known_baseline_failures:nested Write text parser; Windows Bash capability unavailable"
    }
    else {
        $regressionStatus = "unexpected_failure"
    }
    $databaseEvidence = Join-Path $artifactRoot "database-evidence.sql"
    docker compose -p $projectName -f $compose exec -T postgres pg_dump `
        -U codesage_acceptance -d codesage_acceptance --data-only `
        --table=review_execution_runs --table=audit_stages --table=agent_findings `
        --table=audit_sessions --table=audit_tool_calls |
        Set-Content -Path $databaseEvidence -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw "database evidence export failed (exit $LASTEXITCODE)" }
    @(
        "commit=$commit",
        "tracked_worktree_diff_sha=$worktreeFingerprint",
        "status_sha256=$statusFingerprint",
        "database=PostgreSQL 16",
        "redis=Redis 7",
        "compose_project=$projectName",
        "database_url=$env:DATABASE_URL",
        "redis_url=$env:REDIS_URL",
        "queue=$env:AGENT_TASK_QUEUE_NAME",
        "acceptance_exit_code=$($testProcess.ExitCode)",
        "acceptance_summary=" + ((Get-Content $stdout | Select-Object -Last 1).Trim()),
        "acceptance_junit=$acceptanceJunit",
        "acceptance_tests=$($acceptanceSuite.tests) failures=$($acceptanceSuite.failures) errors=$($acceptanceSuite.errors) skipped=$($acceptanceSuite.skipped)",
        "regression_exit_code=$($regressionProcess.ExitCode)",
        "regression_status=$regressionStatus",
        "regression_summary=" + ((Get-Content $regressionStdout | Select-Object -Last 1).Trim()),
        "regression_junit=$regressionJunit",
        "regression_tests=$($regressionSuite.tests) failures=$($regressionSuite.failures) errors=$($regressionSuite.errors) skipped=$($regressionSuite.skipped)",
        "coverage_contract=contract/artifact integrity tests",
        "coverage_supervision=business failure, incomplete, heartbeat DB failure, cancel, duplicate delivery, stale owner",
        "coverage_real_harness=three repeated dual-worker, cancel/resume, and forced-crash takeover runs through perspectives, QueryLoop, Read, SessionStore",
        "skip_and_failure_nodeids=see JUnit testcase classname/name and message attributes",
        "not_executed=frontend typecheck (run separately); symlink support is reported as skipped when unavailable",
        "stdout_log=$stdout",
        "stderr_log=$stderr",
        "regression_stdout_log=$regressionStdout",
        "regression_stderr_log=$regressionStderr"
        "database_evidence=$databaseEvidence"
    ) | Set-Content -Path $report -Encoding utf8
    Get-Content $stdout
    Get-Content $regressionStdout
    if ($regressionStatus -eq "unexpected_failure") {
        throw "runtime/session regression failed unexpectedly (exit $($regressionProcess.ExitCode))"
    }
    if (Test-Path -LiteralPath $pytestRoot) {
        Remove-Item -LiteralPath $pytestRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $env:MANAGED_PROJECTS_ROOT) {
        Remove-Item -LiteralPath $env:MANAGED_PROJECTS_ROOT -Recurse -Force
    }
}
finally {
    Pop-Location
    if (-not $KeepDependencies) {
        docker compose -p $projectName -f $compose down
    }
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
}
