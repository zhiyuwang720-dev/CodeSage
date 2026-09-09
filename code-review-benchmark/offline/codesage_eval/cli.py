from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from codesage_eval.compare import paired_bootstrap, performance_comparison_mode, validate_comparable
from codesage_eval.calibration import run_calibration
from codesage_eval.contracts import DatasetCase, EvalCaseResult, EvalRunManifest, JudgmentRecord
from codesage_eval.dataset import load_dataset, prepare_detached_worktree, select_suite
from codesage_eval.judge import OpenAICompatiblePairJudge, judge_case
from codesage_eval.report import build_summary, write_report
from codesage_eval.runner import ControlPlaneHttpAdapter, run_cases
from codesage_eval.storage import read_jsonl, write_jsonl_atomic
from codesage_eval.fixtures import fetch_current_pr_fixtures
from codesage_eval.doctor import run_doctor
from codesage_eval.bootstrap import ensure_eval_projects, login
from codesage_eval.doctor import run_doctor

OFFLINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = OFFLINE_ROOT.parents[1]
DEFAULT_DATASET = OFFLINE_ROOT / "results" / "benchmark_data.json"
DEFAULT_FIXTURE_MAP = OFFLINE_ROOT / "fixtures" / "current-fixtures.json"


def _json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _git_fingerprints() -> tuple[str, str]:
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
        status = subprocess.check_output(["git", "status", "--porcelain=v1", "-uall"], cwd=REPO_ROOT)
        return commit, hashlib.sha256(status).hexdigest()
    except (OSError, subprocess.CalledProcessError):
        return os.getenv("CODESAGE_CODE_COMMIT", "container-build"), hashlib.sha256(b"").hexdigest()


def command_prepare(args) -> None:
    fixture_map = Path(args.fixture_map or DEFAULT_FIXTURE_MAP)
    if not fixture_map.exists():
        raise SystemExit(
            "fixture map is missing. Fetch current smoke fixtures with:\n"
            "docker compose --profile eval run --rm eval-cli fixtures fetch --suite smoke"
        )
    overrides = _json(fixture_map)
    cases = load_dataset(args.dataset, overrides, fixture_base=fixture_map.parent)
    selected = select_suite(cases, args.suite)
    selected = [prepare_detached_worktree(item, args.fixtures_root) for item in selected]
    write_jsonl_atomic(args.output, selected)
    verified = sum(item.source_mode != "fixture_unverified" for item in selected)
    print(f"prepared {len(selected)} cases ({verified} verified) at {args.output}")


def command_fixtures_fetch(args) -> None:
    all_cases = load_dataset(args.dataset)
    selected = select_suite(all_cases, args.suite) if not args.case else [item for item in all_cases if item.case_id == args.case]
    if args.case and not selected:
        raise SystemExit(f"unknown case id: {args.case}")
    fetched = fetch_current_pr_fixtures(
        dataset_path=args.dataset,
        output_map=args.output_map,
        fixtures_root=args.fixtures_root,
        case_ids={item.case_id for item in selected},
    )
    print(f"fetched {len(selected)} current_pr/diff_only fixtures into {args.output_map}")
    print("These fixtures are suitable for smoke testing, but are not a certified historical baseline.")


def command_doctor(args) -> None:
    result = run_doctor(api_url=args.api_url, phoenix_url=args.phoenix_url, timeout_seconds=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["healthy"]:
        raise SystemExit(1)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def command_run_live(args) -> None:
    if not args.allow_model_calls:
        raise SystemExit("real review is disabled; pass --allow-model-calls after confirming the 30K budget")
    if args.token_budget > 30000:
        raise SystemExit("run-live has a hard maximum of 30000 tokens")
    fixture_map = Path(args.fixture_map or DEFAULT_FIXTURE_MAP)
    if not fixture_map.exists():
        raise SystemExit(
            f"fixture map is missing; run: docker compose --profile eval run --rm eval-cli fixtures fetch --case {args.case}"
        )
    cases = load_dataset(args.dataset, _json(fixture_map), fixture_base=fixture_map.parent)
    selected = [item for item in cases if item.case_id == args.case]
    if len(selected) != 1:
        raise SystemExit(f"unknown case id: {args.case}")
    case = prepare_detached_worktree(selected[0], args.fixtures_root)
    if case.source_mode == "fixture_unverified":
        raise SystemExit(f"case {case.case_id} has no verified fixture")
    api_url = args.api_url
    token = login(api_url=api_url, email=args.email, password=args.password)
    projects = ensure_eval_projects(api_url=api_url, token=token, cases=[case], projects_root=args.projects_root)
    commit, dirty = _git_fingerprints()
    eval_run_id = args.eval_run_id or f"eval-{uuid4()}"
    model_parameters = {
        "provider": os.getenv("LLM_PROVIDER"), "model": os.getenv("LLM_MODEL"),
        "base_url": os.getenv("LLM_BASE_URL"), "token_budget": args.token_budget,
        "per_perspective_token_budget": min(10000, args.token_budget // 3), "max_iterations": 8,
    }
    manifest = EvalRunManifest(
        eval_run_id=eval_run_id, suite="live-single", case_ids=[case.case_id],
        dataset_sha256=hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
        code_commit=commit, code_dirty_sha256=dirty, concurrency=1, timeout_seconds=1800,
        model_fingerprint=_fingerprint(model_parameters), model_parameters=model_parameters,
        prompt_fingerprint=_fingerprint("quick-review-v1"), tool_fingerprint=_fingerprint("runtime-tool-matrices"),
        flow_fingerprint=_fingerprint("quick-review-v1:three-perspectives"),
        budget={"total_tokens": args.token_budget, "per_perspective_tokens": min(10000, args.token_budget // 3)},
        fixture_fingerprints={case.case_id: case.diff_sha256 or case.fixture_sha256 or "missing"},
        baseline_eligible=case.baseline_eligible,
    )
    run_dir = Path(args.runs_root) / eval_run_id
    write_jsonl_atomic(run_dir / "manifest.jsonl", [manifest])
    prepared_path = run_dir / "prepared.jsonl"
    write_jsonl_atomic(prepared_path, [case])
    adapter = ControlPlaneHttpAdapter(
        base_url=api_url, token=token, timeout_seconds=1800, max_iterations=8, token_budget=args.token_budget
    )
    results = asyncio.run(run_cases(cases=[case], eval_run_id=eval_run_id, adapter=adapter,
                                    project_ids=projects, output_path=run_dir / "cases.jsonl", concurrency=1))
    try:
        from codesage_eval.phoenix_adapter import PhoenixAdapter
        phoenix = PhoenixAdapter(base_url=args.phoenix_url)
        spans = phoenix.wait_for_trace(
            project_identifier=args.phoenix_project, eval_run_id=eval_run_id, case_id=case.case_id,
            timeout_seconds=args.trace_timeout,
        )
    except Exception as exc:
        print(f"warning: Phoenix trace lookup failed: {exc}")
        spans = []
    for item in results:
        item.trace_complete = bool(spans)
        item.trace_id = phoenix.trace_id(spans) if spans else None
    write_jsonl_atomic(run_dir / "cases.jsonl", results)
    manifest.task_run_trace_map = {
        item.case_id: {"task_id": item.task_id, "review_run_id": item.review_run_id, "trace_id": item.trace_id}
        for item in results
    }
    write_jsonl_atomic(run_dir / "manifest.jsonl", [manifest])
    write_jsonl_atomic(run_dir / "findings.jsonl", (
        {"eval_run_id": eval_run_id, "case_id": item.case_id, **finding.model_dump(mode="json")}
        for item in results for finding in item.candidates
    ))
    print(run_dir)


def _create_manifest(args, cases: list[DatasetCase]) -> EvalRunManifest:
    commit, dirty = _git_fingerprints()
    dataset_hash = hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest()
    return EvalRunManifest(
        eval_run_id=args.eval_run_id or f"eval-{uuid4()}",
        suite=args.suite,
        case_ids=[item.case_id for item in cases],
        dataset_sha256=dataset_hash,
        code_commit=commit,
        code_dirty_sha256=dirty,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout,
        model_fingerprint=args.model_fingerprint,
        model_parameters=_json(args.model_parameters_json) if args.model_parameters_json else {},
        prompt_fingerprint=args.prompt_fingerprint,
        tool_fingerprint=args.tool_fingerprint,
        flow_fingerprint=args.flow_fingerprint,
        price_table_version=args.price_table_version,
        budget=_json(args.budget_json) if args.budget_json else {},
        hardware_fingerprint=args.hardware_fingerprint,
        baseline_eligible=all(item.baseline_eligible for item in cases),
    )


def command_run(args) -> Path:
    if not args.allow_model_calls:
        raise SystemExit("real review is disabled; pass --allow-model-calls after confirming budget")
    cases = [item for item in read_jsonl(args.prepared, DatasetCase)]
    modes = {item.source_mode for item in cases}
    if "fixture_unverified" in modes:
        raise SystemExit("prepared suite contains fixture_unverified cases; run prepare with fixed fixtures")
    if len(modes) != 1:
        raise SystemExit("full_source and diff_only cases cannot be mixed in one comparable run")
    manifest = _create_manifest(args, cases)
    manifest.fixture_fingerprints = {
        item.case_id: item.diff_sha256 or item.fixture_sha256 or "missing" for item in cases
    }
    manifest.baseline_eligible = all(item.baseline_eligible for item in cases)
    missing = [name for name in ("model_fingerprint", "prompt_fingerprint", "tool_fingerprint", "flow_fingerprint") if not getattr(manifest, name)]
    if missing:
        raise SystemExit(f"baseline fingerprint fields are required: {', '.join(missing)}")
    run_dir = Path(args.runs_root) / manifest.eval_run_id
    write_jsonl_atomic(run_dir / "manifest.jsonl", [manifest])
    adapter = ControlPlaneHttpAdapter(
        base_url=args.api_url, token=args.api_token, timeout_seconds=args.timeout
    )
    results = asyncio.run(
        run_cases(
            cases=cases,
            eval_run_id=manifest.eval_run_id,
            adapter=adapter,
            project_ids=_json(args.project_map),
            output_path=run_dir / "cases.jsonl",
            concurrency=args.concurrency,
        )
    )
    manifest.task_run_trace_map = {
        item.case_id: {
            "task_id": item.task_id,
            "review_run_id": item.review_run_id,
            "trace_id": item.trace_id,
        }
        for item in results
    }
    write_jsonl_atomic(run_dir / "manifest.jsonl", [manifest])
    write_jsonl_atomic(
        run_dir / "findings.jsonl",
        (
            {"eval_run_id": manifest.eval_run_id, "case_id": result.case_id, **finding.model_dump(mode="json")}
            for result in results
            for finding in result.candidates
        ),
    )
    print(run_dir)
    return run_dir


def command_judge(args) -> None:
    run_dir = Path(args.run_dir)
    manifest = read_jsonl(run_dir / "manifest.jsonl", EvalRunManifest)[0]
    cases = {item.case_id: item for item in read_jsonl(args.prepared, DatasetCase)}
    results = [item for item in read_jsonl(run_dir / "cases.jsonl", EvalCaseResult)]
    judge = OpenAICompatiblePairJudge(base_url=args.judge_base_url, api_key=args.judge_api_key, model=args.judge_model)
    records = []
    for result in results:
        records.extend(
            judge_case(
                eval_run_id=manifest.eval_run_id,
                case_id=result.case_id,
                golden=cases[result.case_id].golden,
                candidates=result.candidates,
                judge=judge,
                cache_path=args.cache,
            )
        )
    write_jsonl_atomic(run_dir / "judgments.jsonl", records)
    manifest.judge_fingerprint = judge.fingerprint
    write_jsonl_atomic(run_dir / "manifest.jsonl", [manifest])


def command_report(args) -> None:
    run_dir = Path(args.run_dir)
    cases = [item for item in read_jsonl(args.prepared, DatasetCase)]
    results = [item for item in read_jsonl(run_dir / "cases.jsonl", EvalCaseResult)]
    judgments = [item for item in read_jsonl(run_dir / "judgments.jsonl", JudgmentRecord)]
    output = write_report(run_dir, build_summary(cases, results, judgments), public=args.public)
    print(output)


def command_compare(args) -> None:
    baseline_manifest = read_jsonl(Path(args.baseline) / "manifest.jsonl")[0]
    candidate_manifest = read_jsonl(Path(args.candidate) / "manifest.jsonl")[0]
    mismatches = validate_comparable(baseline_manifest, candidate_manifest)
    if mismatches:
        raise SystemExit(f"incomparable manifests: {', '.join(mismatches)}")
    baseline = read_jsonl(Path(args.baseline) / "summary.jsonl")[0]
    candidate = read_jsonl(Path(args.candidate) / "summary.jsonl")[0]
    baseline_f1 = {item["case_id"]: float(item["f1"]) for item in baseline["cases"]}
    candidate_f1 = {item["case_id"]: float(item["f1"]) for item in candidate["cases"]}
    comparison = paired_bootstrap(baseline_f1, candidate_f1)
    comparison["performance_mode"] = performance_comparison_mode(
        baseline_manifest, candidate_manifest
    )
    write_jsonl_atomic(Path(args.candidate) / "comparison.jsonl", [comparison])
    print(json.dumps(comparison, ensure_ascii=False))


def command_all(args) -> None:
    if not Path(args.prepared).exists():
        raise SystemExit(f"prepared fixture manifest is missing; run: codesage-eval prepare --output {args.prepared}")
    run_dir = command_run(args)
    args.run_dir = str(run_dir)
    command_judge(args)
    command_report(args)


def command_cleanup_legacy(args) -> None:
    results_root = (OFFLINE_ROOT / "results").resolve()
    targets: list[Path] = []
    for raw in args.path:
        target = Path(raw).resolve()
        if target != results_root and results_root not in target.parents:
            raise SystemExit(f"refusing cleanup outside {results_root}: {target}")
        targets.append(target)
    print("legacy cleanup targets:")
    for target in targets:
        print(f"  {'delete' if args.apply else 'would delete'}: {target}")
    if not args.apply:
        print("dry run only; pass --apply to remove exactly the listed paths")
        return
    for target in targets:
        if target.is_dir():
            import shutil

            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def command_calibrate(args) -> None:
    pairs = read_jsonl(args.pairs)
    judge = OpenAICompatiblePairJudge(
        base_url=args.judge_base_url, api_key=args.judge_api_key, model=args.judge_model
    )
    write_jsonl_atomic(args.output, [run_calibration(pairs, judge)])
    print(args.output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codesage-eval")
    commands = parser.add_subparsers(dest="command", required=True)
    fixtures = commands.add_parser("fixtures")
    fixture_commands = fixtures.add_subparsers(dest="fixture_command", required=True)
    fetch = fixture_commands.add_parser("fetch")
    fetch.add_argument("--dataset", default=str(DEFAULT_DATASET))
    fetch.add_argument("--suite", choices=["smoke", "calibration", "holdout", "full"], default="smoke")
    fetch.add_argument("--case")
    fetch.add_argument("--output-map", default=str(DEFAULT_FIXTURE_MAP))
    fetch.add_argument("--fixtures-root", default=str(OFFLINE_ROOT / "fixtures"))
    fetch.set_defaults(func=command_fixtures_fetch)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--api-url", default=os.getenv("CODESAGE_EVAL_API_URL", "http://eval-api:8000"))
    doctor.add_argument("--phoenix-url", default=os.getenv("CODESAGE_PHOENIX_URL", "http://phoenix:6006"))
    doctor.add_argument("--timeout", type=float, default=30)
    doctor.set_defaults(func=command_doctor)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--dataset", default=str(DEFAULT_DATASET))
    prepare.add_argument("--fixture-map")
    prepare.add_argument("--suite", choices=["smoke", "calibration", "holdout", "full"], default="smoke")
    prepare.add_argument("--output", default=str(OFFLINE_ROOT / "prepared" / "dataset.jsonl"))
    prepare.add_argument("--fixtures-root", default=str(OFFLINE_ROOT / "fixtures"))
    prepare.set_defaults(func=command_prepare)

    run = commands.add_parser("run")
    run.add_argument("--prepared", required=True)
    run.add_argument("--dataset", default=str(DEFAULT_DATASET))
    run.add_argument("--suite", default="smoke")
    run.add_argument("--runs-root", default=str(OFFLINE_ROOT / "runs"))
    run.add_argument("--eval-run-id")
    run.add_argument("--api-url", required=True)
    run.add_argument("--api-token", required=True)
    run.add_argument("--project-map", required=True)
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--timeout", type=int, default=3600)
    run.add_argument("--model-fingerprint", required=True)
    run.add_argument("--prompt-fingerprint", required=True)
    run.add_argument("--tool-fingerprint", required=True)
    run.add_argument("--flow-fingerprint", required=True)
    run.add_argument("--model-parameters-json")
    run.add_argument("--budget-json")
    run.add_argument("--hardware-fingerprint")
    run.add_argument("--price-table-version")
    run.add_argument("--allow-model-calls", action="store_true")
    run.set_defaults(func=command_run)

    live = commands.add_parser("run-live", help="run exactly one opt-in live review without a judge")
    live.add_argument("--case", required=True)
    live.add_argument("--dataset", default=str(DEFAULT_DATASET))
    live.add_argument("--fixture-map")
    live.add_argument("--fixtures-root", default=str(OFFLINE_ROOT / "fixtures"))
    live.add_argument("--projects-root", default="/workspace/projects")
    live.add_argument("--runs-root", default=str(OFFLINE_ROOT / "runs"))
    live.add_argument("--eval-run-id")
    live.add_argument("--api-url", default=os.getenv("CODESAGE_EVAL_API_URL", "http://eval-api:8000"))
    live.add_argument("--email", default=os.getenv("CODESAGE_EVAL_EMAIL", "demo@example.com"))
    live.add_argument("--password", default=os.getenv("CODESAGE_EVAL_PASSWORD", "demo123"))
    live.add_argument("--phoenix-url", default=os.getenv("CODESAGE_PHOENIX_URL", "http://phoenix:6006"))
    live.add_argument("--phoenix-project", default=os.getenv("CODESAGE_PHOENIX_PROJECT", "codesage-eval"))
    live.add_argument("--trace-timeout", type=float, default=30)
    live.add_argument("--token-budget", type=int, default=30000)
    live.add_argument("--allow-model-calls", action="store_true")
    live.set_defaults(func=command_run_live)

    judge = commands.add_parser("judge")
    judge.add_argument("--run-dir", required=True)
    judge.add_argument("--prepared", required=True)
    judge.add_argument("--judge-base-url", required=True)
    judge.add_argument("--judge-api-key", required=True)
    judge.add_argument("--judge-model", required=True)
    judge.add_argument("--cache", default=str(OFFLINE_ROOT / ".cache" / "judge.jsonl"))
    judge.set_defaults(func=command_judge)

    report = commands.add_parser("report")
    report.add_argument("--run-dir", required=True)
    report.add_argument("--prepared", required=True)
    report.add_argument("--public", action="store_true")
    report.set_defaults(func=command_report)

    compare = commands.add_parser("compare")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.set_defaults(func=command_compare)

    all_command = commands.add_parser("all", help="run, judge and report using already prepared fixtures")
    all_command.add_argument("--prepared", required=True)
    all_command.add_argument("--dataset", default=str(DEFAULT_DATASET))
    all_command.add_argument("--suite", default="smoke")
    all_command.add_argument("--runs-root", default=str(OFFLINE_ROOT / "runs"))
    all_command.add_argument("--eval-run-id")
    all_command.add_argument("--api-url", required=True)
    all_command.add_argument("--api-token", required=True)
    all_command.add_argument("--project-map", required=True)
    all_command.add_argument("--concurrency", type=int, default=2)
    all_command.add_argument("--timeout", type=int, default=3600)
    all_command.add_argument("--model-fingerprint", required=True)
    all_command.add_argument("--prompt-fingerprint", required=True)
    all_command.add_argument("--tool-fingerprint", required=True)
    all_command.add_argument("--flow-fingerprint", required=True)
    all_command.add_argument("--model-parameters-json")
    all_command.add_argument("--budget-json")
    all_command.add_argument("--hardware-fingerprint")
    all_command.add_argument("--price-table-version")
    all_command.add_argument("--allow-model-calls", action="store_true")
    all_command.add_argument("--judge-base-url", required=True)
    all_command.add_argument("--judge-api-key", required=True)
    all_command.add_argument("--judge-model", required=True)
    all_command.add_argument("--cache", default=str(OFFLINE_ROOT / ".cache" / "judge.jsonl"))
    all_command.add_argument("--public", action="store_true")
    all_command.set_defaults(func=command_all)

    cleanup = commands.add_parser("cleanup-legacy")
    cleanup.add_argument("--path", action="append", required=True, help="exact path below offline/results")
    cleanup.add_argument("--apply", action="store_true")
    cleanup.set_defaults(func=command_cleanup_legacy)

    calibrate = commands.add_parser("calibrate", help="run the fixed human-labelled pairs twice without cache")
    calibrate.add_argument(
        "--pairs", default=str(OFFLINE_ROOT / "codesage_eval" / "data" / "calibration_v1.jsonl")
    )
    calibrate.add_argument("--judge-base-url", required=True)
    calibrate.add_argument("--judge-api-key", required=True)
    calibrate.add_argument("--judge-model", required=True)
    calibrate.add_argument("--output", required=True)
    calibrate.set_defaults(func=command_calibrate)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)
