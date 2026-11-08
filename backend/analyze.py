#!/usr/bin/env python
"""Paired analysis of one or more sweeps.

    python analyze.py                       # the latest sweep
    python analyze.py --sweeps id1,id2,id3  # e.g. one sweep per log length
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from incident_response.analysis import analyze
from incident_response.db import connect, init_db, latest_sweep

BACKEND_DIR = Path(__file__).resolve().parent


def pair(summary: dict, key: str) -> str:
    rate = summary[key]["rate"]
    return "  n/a" if rate is None else f"{rate:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sweeps", default="", help="comma-separated sweep ids (default: latest)")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--db", type=Path, default=BACKEND_DIR / "data" / "runs.db")
    args = parser.parse_args()

    conn = connect(args.db)
    init_db(conn)
    sweep_ids = [item.strip() for item in args.sweeps.split(",") if item.strip()]
    if not sweep_ids:
        latest = latest_sweep(conn)
        if latest is None:
            sys.exit("no sweeps recorded yet")
        sweep_ids = [latest]

    result = analyze(conn, sweep_ids)
    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(f"Sweeps: {', '.join(sweep_ids)}")
    print("Each pair below is baseline -> pipeline. p is exact McNemar on the paired logs.\n")
    for arm in result["arms"]:
        single, pipeline, paired = arm["single_agent"], arm["pipeline"], arm["paired"]
        print(f"{arm['provider']}:{arm['model']}  requests={arm['request_count']}  pairs={arm['n_pairs']}")
        for label, key, paired_key in (
            ("accuracy", "accuracy", "correct"),
            ("meets proposal", "meets_proposal", "meets_proposal"),
            ("grounded", "grounded", "grounded"),
        ):
            stats = paired[paired_key]
            print(
                f"  {label:<15} {pair(single, key)} -> {pair(pipeline, key)}"
                f"   only baseline={stats['only_single']}  only pipeline={stats['only_pipeline']}  p={stats['p']:.3f}"
            )
        if single["recovery"]["n"] or pipeline["recovery"]["n"]:
            stats = paired["recovery_valid"]
            print(
                f"  {'recovery':<15} {pair(single, 'recovery')} -> {pair(pipeline, 'recovery')}"
                f"   only baseline={stats['only_single']}  only pipeline={stats['only_pipeline']}  p={stats['p']:.3f}"
                f"   right fault, bad fix: {single['invalid_after_correct']} -> {pipeline['invalid_after_correct']}"
            )
        print(f"  {'brier':<15} {single['brier']} -> {pipeline['brier']}")
        print(f"  {'labels base':<15} {single['failure_labels'] or 'none'}")
        print(f"  {'labels pipe':<15} {pipeline['failure_labels'] or 'none'}")
        print(f"  {'tokens in':<15} {single['mean_input_tokens']} -> {pipeline['mean_input_tokens']}")
        print(f"  {'tokens out':<15} {single['mean_output_tokens']} -> {pipeline['mean_output_tokens']}")
        for name, other in arm["others"].items():
            summary = other["summary"]
            stats = other["vs_single"]
            print(
                f"  {name}: accuracy {pair(summary, 'accuracy')}  grounded {pair(summary, 'grounded')}  "
                f"llm calls {summary['mean_llm_calls']}  tokens {summary['mean_input_tokens']}/{summary['mean_output_tokens']}  "
                f"vs baseline only-baseline={stats['only_single']} only-this={stats['only_pipeline']} p={stats['p']:.3f}"
            )
        print()


if __name__ == "__main__":
    main()
