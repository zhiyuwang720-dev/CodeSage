param([switch]$KeepDependencies)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Resolve-Path (Join-Path $here "..\..")
$compose = Join-Path $here "docker-compose.yml"
$artifactRoot = Join-Path $backend ".acceptance-artifacts"
$report = Join-Path $artifactRoot "report.txt"
New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null

$env:DATABASE_URL = "postgresql+asyncpg://codesage_acceptance:codesage_acceptance@127.0.0.1:55432/codesage_acceptance"
$env:REDIS_URL = "redis://127.0.0.1:56379/0"
$env:AGENT_TASK_QUEUE_NAME = "codesage:acceptance:agent_tasks"
$env:AGENT_TASK_EXECUTION_MODE = "worker"
$env:AGENT_WORKER_CONCURRENCY = "1"
$env:CODESAGE_WORKER_ACCEPTANCE = "1"
$env:CODESAGE_ACCEPTANCE_ARTIFACT_ROOT = $artifactRoot
$pytestRoot = ($artifactRoot -replace '\\', '/') + "/pytest"
$env:PYTEST_ADDOPTS = "--basetemp=$pytestRoot"
$env:PYTHONWARNINGS = "ignore"

Push-Location $backend
try {
    docker compose -f $compose up -d --wait
    alembic upgrade head
    $commit = git rev-parse HEAD
    $stdout = Join-Path $artifactRoot "pytest.stdout.log"
    $stderr = Join-Path $artifactRoot "pytest.stderr.log"
    $testProcess = Start-Process -FilePath "python" -ArgumentList @(
        "-m", "pytest", "tests/worker_acceptance", "-q"
    ) -Wait -PassThru -NoNewWindow -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    @(
        "commit=$commit",
        "database=PostgreSQL 16",
        "redis=Redis 7",
        "queue=$env:AGENT_TASK_QUEUE_NAME",
        "pytest_exit_code=$($testProcess.ExitCode)",
        "pytest_summary=" + ((Get-Content $stdout | Select-Object -Last 1).Trim()),
        "stdout_log=$stdout",
        "stderr_log=$stderr"
    ) | Set-Content -Path $report -Encoding utf8
    Get-Content $stdout
    if ($testProcess.ExitCode -ne 0) { throw "worker acceptance failed (exit $($testProcess.ExitCode))" }
    if (Test-Path -LiteralPath $pytestRoot) {
        Remove-Item -LiteralPath $pytestRoot -Recurse -Force
    }
}
finally {
    Pop-Location
    if (-not $KeepDependencies) {
        docker compose -f $compose down
    }
}
