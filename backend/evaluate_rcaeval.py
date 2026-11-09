#!/usr/bin/env python
"""Run a pilot on RCAEval RE3: which service caused the failure?

    python evaluate_rcaeval.py --per-system 10 --arm rules
    python evaluate_rcaeval.py --per-system 10 --arm groq:qwen/qwen3.8-27b

Results are written as JSON lines to outputs/rcaeval_<arm>.jsonl.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from incident_response.analysis import exact_mcnemar, wilson_interval
from incident_response.providers import get_client
from incident_response.rcaeval import MAX_TOKENS, list_cases, load_case, parse_ranking, payload_for, pilot_selection, score, system_prompt

BACKEND = Path(__file__).resolve().parent
DATA = BACKEND / "external" / "rcaeval"
WAITS = (5.0, 15.0, 30.0, 60.0, 60.0, 60.0)
SYSTEMS = ("RE3-OB", "RE3-SS", "RE3-TT")


def call(client, system: str, payload: str):
    for wait in (*WAITS, None):
        response = client.complete(system, payload, max_tokens=MAX_TOKENS)
        error = (response.error or "").lower()
        if "429" not in error or "per day" in error or wait is None:
            return response
        time.sleep(wait)
    return response  # pragma: no cover


def report(records: list[dict], conditions: list[str]) -> None:
    for condition in conditions:
        rows = [r for r in records if r["condition"] == condition and not r.get("api_error")]
        n = len(rows)
        top1, top3 = sum(r["top1"] for r in rows), sum(r["top3"] for r in rows)
        failed = sum(1 for r in records if r["condition"] == condition and r.get("api_error"))
        tokens = f"  tokens {sum(r['input_tokens'] for r in rows) / max(1, n):.0f}/{sum(r['output_tokens'] for r in rows) / max(1, n):.0f}" if condition != "rules" else ""
        print(f"{condition:<12} n={n} failed={failed} Top-1 {top1}/{n} {wilson_interval(top1, n)}  Top-3 {top3}/{n}  malformed {sum(r.get('malformed', False) for r in rows)}{tokens}")
        for system in SYSTEMS:
            sub = [r for r in rows if r["system"] == system]
            if sub:
                print(f"    {system}: Top-1 {sum(r['top1'] for r in sub)}/{len(sub)}  Top-3 {sum(r['top3'] for r in sub)}/{len(sub)}")
    if {"raw_sample", "summary"} <= set(conditions):
        a = {r["case_id"]: r for r in records if r["condition"] == "raw_sample" and not r.get("api_error")}
        b = {r["case_id"]: r for r in records if r["condition"] == "summary" and not r.get("api_error")}
        common = a.keys() & b.keys()
        only_a = sum(a[k]["top1"] and not b[k]["top1"] for k in common)
        only_b = sum(b[k]["top1"] and not a[k]["top1"] for k in common)
        print(f"paired Top-1, n={len(common)}: only raw={only_a}  only summary={only_b}  p={exact_mcnemar(only_a, only_b):.4f}")


def main() -> None:
    load_dotenv(BACKEND / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--per-system", type=int, default=10)
    parser.add_argument("--arm", required=True, help="rules, or provider:model")
    parser.add_argument("--systems", default=",".join(SYSTEMS))
    args = parser.parse_args()

    rules = args.arm == "rules"
    client = None
    if not rules:
        provider, _, model = args.arm.partition(":")
        client = get_client(provider, model or None)
    conditions = ["rules"] if rules else ["raw_sample", "summary"]

    selected = []
    for system in args.systems.split(","):
        archive = DATA / f"{system}.zip"
        if not archive.exists():
            sys.exit(f"missing {archive}")
        selected += [(str(archive), root) for root in pilot_selection(list_cases(str(archive)), args.per_system)]

    # Spread systems evenly, so a run stopped by a quota stays balanced.
    selected.sort(key=lambda item: item[1].rsplit("/", 2)[-1])
    out = BACKEND / "outputs" / f"rcaeval_{args.arm.replace(':', '_').replace('/', '_')}.jsonl"
    out.parent.mkdir(exist_ok=True)
    records: list[dict] = []
    stop = False
    with out.open("w", encoding="utf-8") as sink:
        for index, (archive, root) in enumerate(selected, 1):
            case = load_case(archive, root)
            if case.root_cause not in case.services:
                print(f"note: {case.case_id} root cause has no log lines")
            for condition in conditions:
                record = {"case_id": case.case_id, "system": case.system, "fault": case.fault, "root_cause": case.root_cause, "condition": condition, "n_candidates": len(case.services), "total_lines": case.total_lines}
                if rules:
                    ranked = case.rules_ranking
                    record.update(ranked=ranked[:3])
                else:
                    response = call(client, system_prompt(case), payload_for(case, condition))
                    parsed = parse_ranking(response, case.services)
                    ranked = parsed["ranked"]
                    record.update(parsed, ranked=ranked[:3], input_tokens=response.input_tokens, output_tokens=response.output_tokens)
                    if parsed["api_error"] and "per day" in (parsed["error"] or "").lower():
                        stop = True
                record["top1"], record["top3"] = score(ranked, case.root_cause)
                records.append(record)
                sink.write(json.dumps(record) + "\n")
                sink.flush()
                print(f"[{index}/{len(selected)}] {case.case_id:<38} {condition:<11} {'ok ' if record['top1'] else ('top3' if record['top3'] else 'miss')} -> {ranked[:3]}"[:150])
                if stop:
                    break
            if stop:
                print("stopped: daily quota reached")
                break
    print()
    report(records, conditions)


if __name__ == "__main__":
    main()
