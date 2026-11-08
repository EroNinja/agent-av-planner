#!/usr/bin/env python
"""Side-by-side evaluation across logs, models and conditions.

Proposal-compatible form (single model, both conditions):
    python evaluate.py --logs-dir logs/ --output outputs/comparison_results.csv

The cross-model form -- the capability axis:
    python evaluate.py --arms anthropic:claude-opus-5-5,groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from dotenv import load_dotenv

from incident_response.conditions import ALL_CONDITIONS, CONDITIONS
from incident_response.db import connect, init_db
from incident_response.evaluation import Arm, plan_size, run_sweep_stream, summarize_cells, validate_arms
from incident_response.log_generator import discover_logs
from incident_response.providers import PROVIDERS

BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_LOGS_DIR = BACKEND_DIR / "logs"
DEFAULT_OUTPUT = BACKEND_DIR / "outputs" / "comparison_results.csv"

CSV_COLUMNS = [
    "sweep_id", "log_name", "true_fault", "arm", "provider", "model", "condition",
    "predicted_fault", "correct", "strict_correct", "confidence", "malformed", "api_error", "evidence_score",
    "evidence_claims", "latency_ms",
]


def parse_arms(raw: str) -> list[Arm]:
    arms: list[Arm] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        provider, _, model = token.partition(":")
        provider = provider.strip()
        if provider not in PROVIDERS:
            sys.exit(f"unknown provider '{provider}' in --arms (have: {', '.join(sorted(PROVIDERS))})")
        arms.append(Arm(provider=provider, model=model.strip() or PROVIDERS[provider]["default_model"]))
    if not arms:
        sys.exit("--arms produced no valid entries")
    try:
        validate_arms(arms)
    except ValueError as exc:
        sys.exit(str(exc))
    return arms


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs-dir", type=Path, default=DEFAULT_LOGS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--arms",
        default="anthropic:",
        help="comma-separated provider:model pairs (model optional)",
    )
    parser.add_argument("--conditions", default=",".join(CONDITIONS))
    parser.add_argument("--recovery", action="store_true", help="also score a proposed recovery action")
    parser.add_argument("--no-hint", action="store_true", help="remove the spread-versus-clustered rule from the prompt")
    parser.add_argument("--compound", action="store_true", help="allow more than one fault per run")
    parser.add_argument("--limit", type=int, default=0, help="cap the number of logs (0 = all)")
    parser.add_argument("--db", type=Path, default=BACKEND_DIR / "data" / "runs.db")
    args = parser.parse_args()

    records = discover_logs(args.logs_dir)
    if not records:
        sys.exit(f"no logs with ground truth found in {args.logs_dir} -- run generate_logs.py --suite first")
    if args.limit:
        records = records[: args.limit]

    arms = parse_arms(args.arms)
    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    for condition in conditions:
        if condition not in ALL_CONDITIONS:
            sys.exit(f"unknown condition '{condition}'")

    conn = connect(args.db)
    init_db(conn)

    total = plan_size(len(records), arms, conditions)
    print(f"Evaluating {len(records)} logs x {len(arms)} arm(s) x {len(conditions)} condition(s) = {total} LLM calls\n")

    cells: list[dict] = []
    sweep_id = "unknown"
    for event in run_sweep_stream(records, arms, conn, conditions=conditions, recovery=args.recovery, hint=not args.no_hint, compound=args.compound):
        kind = event["type"]
        if kind == "arm_start":
            print(f"=== {event['arm']} ===")
        elif kind == "arm_failed":
            print(f"!!! {event['arm']} skipped: {event['error']}")
        elif kind == "cell":
            cells.append(event)
            mark = "FAIL" if event["api_error"] else ("ok  " if event["correct"] else "MISS")
            note = " [malformed]" if event["malformed"] else ""
            print(
                f"  [{event['completed']:>3}/{event['total']}] {mark} {event['log_name']:<28}"
                f" {event['condition']:<13} -> {event['predicted_fault']}{note}"
            )
        elif kind == "sweep_done":
            sweep_id = event["sweep_id"]

    if not cells:
        sys.exit("no cells completed -- check your API keys")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for cell in cells:
            provider, _, model = cell["arm"].partition(":")
            evidence = cell.get("evidence") or {}
            writer.writerow(
                {
                    "sweep_id": cell["sweep_id"],
                    "log_name": cell["log_name"],
                    "true_fault": cell["true_fault"],
                    "arm": cell["arm"],
                    "provider": provider,
                    "model": model,
                    "condition": cell["condition"],
                    "predicted_fault": cell["predicted_fault"],
                    "correct": int(bool(cell["correct"])),
                    "strict_correct": int(bool(cell["strict_correct"])),
                    "confidence": cell["confidence"],
                    "malformed": int(bool(cell["malformed"])),
                    "api_error": int(bool(cell["api_error"])),
                    "evidence_score": evidence.get("score"),
                    "evidence_claims": evidence.get("claims_checked"),
                    "latency_ms": cell["latency_ms"],
                }
            )

    print(f"\nWrote {len(cells)} results to {args.output}")
    print(f"Sweep id: {sweep_id}\n")

    summary = summarize_cells(cells)
    print("=== Accuracy by arm and condition ===")
    print("    acc = fault named correctly; strict = correct AND valid JSON; failed calls excluded")
    for (arm, condition), bucket in sorted(summary.items()):
        n = bucket["n"]
        accuracy = bucket["correct"] / n if n else float("nan")
        strict = bucket["strict"] / n if n else float("nan")
        confidence = (
            bucket["confidence_sum"] / bucket["confidence_n"] if bucket["confidence_n"] else float("nan")
        )
        malformed = bucket["malformed"] / n if n else float("nan")
        print(
            f"  {arm:<36} {condition:<13} acc={accuracy:.3f}  strict={strict:.3f}  conf={confidence:.2f}"
            f"  malformed={malformed:.2f}  (n={n}, failed={bucket['failed']})"
        )

    print("\n=== Per-fault-type accuracy ===")
    for (arm, condition), bucket in sorted(summary.items()):
        parts = [
            f"{fault}={stats['correct'] / stats['n']:.2f} (n={stats['n']})"
            for fault, stats in sorted(bucket["by_fault"].items())
        ]
        print(f"  {arm:<36} {condition:<13} " + "  ".join(parts))

    print("\n=== Pipeline minus baseline (accuracy delta) ===")
    for arm in sorted({arm for arm, _ in summary}):
        single = summary.get((arm, "single_agent"))
        pipeline = summary.get((arm, "pipeline"))
        if not single or not pipeline or not single["n"] or not pipeline["n"]:
            continue
        delta = pipeline["correct"] / pipeline["n"] - single["correct"] / single["n"]
        strict = pipeline["strict"] / pipeline["n"] - single["strict"] / single["n"]
        print(f"  {arm:<36} acc {delta:+.3f}   strict {strict:+.3f}")


if __name__ == "__main__":
    main()
