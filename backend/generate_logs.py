#!/usr/bin/env python
"""Generate fault-injected logs with hidden ground truth.

Proposal-compatible single-scenario form:
    python generate_logs.py --fault-type pool_exhausted --run-id 1

Whole evaluation set in one go:
    python generate_logs.py --suite --runs-per-fault 4
    python generate_logs.py --suite --runs-per-fault 4 --noise
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from incident_response.log_generator import generate_log, generate_suite
from incident_response.taxonomy import COMPOUND_FAULTS, FAULT_TYPES

DEFAULT_LOGS_DIR = Path(__file__).resolve().parent / "logs"


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fault-type", choices=FAULT_TYPES, help="single scenario to generate")
    parser.add_argument("--run-id", type=int, default=1)
    parser.add_argument("--suite", action="store_true", help="generate every fault type")
    parser.add_argument("--runs-per-fault", type=int, default=4)
    parser.add_argument("--requests", type=int, default=30, help="requests per run")
    parser.add_argument("--compound", action="store_true", help="suite only: also generate two-fault runs")
    parser.add_argument(
        "--noise",
        action="store_true",
        help="add unrelated warnings, red-herring transients and irregular cadence",
    )
    parser.add_argument("--logs-dir", type=Path, default=DEFAULT_LOGS_DIR)
    parser.add_argument("--out", type=Path, help="explicit .log path (single scenario only)")
    parser.add_argument("--meta", type=Path, help="explicit .meta.json path (single scenario only)")
    args = parser.parse_args()

    if not args.suite and not args.fault_type:
        parser.error("pass --fault-type, or --suite to generate the whole set")

    if args.suite:
        records = generate_suite(
            args.logs_dir,
            FAULT_TYPES + COMPOUND_FAULTS if args.compound else FAULT_TYPES,
            args.runs_per_fault,
            request_count=args.requests,
            noise=args.noise,
        )
        for record in records:
            print(f"{record.log_path}  (true fault: {record.true_fault})")
        print(f"\nGenerated {len(records)} logs in {args.logs_dir}")
        return

    record = generate_log(
        args.fault_type,
        args.run_id,
        args.logs_dir,
        request_count=args.requests,
        noise=args.noise,
    )

    # Honour explicit --out/--meta by copying the generated pair.
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(record.text(), encoding="utf-8")
        print(args.out)
    else:
        print(record.log_path)

    if args.meta:
        args.meta.parent.mkdir(parents=True, exist_ok=True)
        args.meta.write_text(record.meta_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(args.meta)
    elif not args.out:
        print(record.meta_path)


if __name__ == "__main__":
    main()
