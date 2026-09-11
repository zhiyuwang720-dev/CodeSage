from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.infrastructure.observability.pricing import PricingCatalog, sync_catalog_to_file

from .exporter import export_bundle, summarize_bundle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.diagnostics")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export")
    export.add_argument("--run-id", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--public", action="store_true")

    summarize = sub.add_parser("summarize")
    summarize.add_argument("--input", required=True)

    pricing = sub.add_parser("pricing")
    pricing_sub = pricing.add_subparsers(dest="pricing_command", required=True)
    doctor = pricing_sub.add_parser("doctor")
    doctor.add_argument("--catalog", required=True)
    doctor.add_argument("--provider", required=True)
    doctor.add_argument("--endpoint-id", required=True)
    doctor.add_argument("--model", required=True)
    sync = pricing_sub.add_parser("sync")
    sync.add_argument("--catalog", required=True)
    sync.add_argument("--output", required=True)

    smoke = sub.add_parser("smoke")
    smoke.add_argument("--case-id", required=True)
    smoke.add_argument("--allow-paid", action="store_true")
    smoke.add_argument("--max-cost", required=True)
    smoke.add_argument("--currency", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "export":
        try:
            target, code = export_bundle(run_id=args.run_id, output=args.output, public=args.public)
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps({"output": str(target), "exit_code": code}, sort_keys=True))
        return code
    if args.command == "summarize":
        print(json.dumps(summarize_bundle(args.input), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "pricing":
        catalog = PricingCatalog.from_jsonl(args.catalog)
        if args.pricing_command == "sync":
            print(json.dumps(sync_catalog_to_file(catalog, args.output), ensure_ascii=False, sort_keys=True))
            return 0
        from datetime import datetime, timezone

        print(
            json.dumps(
                catalog.doctor(
                    provider=args.provider,
                    endpoint_id=args.endpoint_id,
                    model=args.model,
                    at=datetime.now(timezone.utc),
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "smoke":
        if not args.allow_paid:
            print("smoke requires --allow-paid", file=sys.stderr)
            return 1
        print("real smoke is not implemented in the offline diagnostics build", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
