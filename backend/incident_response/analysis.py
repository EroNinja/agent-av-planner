"""Paired statistics for the single-agent vs pipeline comparison.

Both conditions see the same logs, so the right test is paired: only logs where
exactly one condition succeeded carry information about the difference. With a
few dozen logs per arm the confidence intervals are wide, and this module
reports them rather than a bare percentage.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict

ECE_BINS = 10


def exact_mcnemar(only_single: int, only_pipeline: int) -> float:
    """Two-sided exact binomial test on the discordant pairs."""
    total = only_single + only_pipeline
    if total == 0:
        return 1.0
    tail = sum(math.comb(total, i) for i in range(min(only_single, only_pipeline) + 1))
    return min(1.0, 2 * tail / 2**total)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def brier_score(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return round(sum((conf - outcome) ** 2 for conf, outcome in pairs) / len(pairs), 4)


def expected_calibration_error(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for conf, outcome in pairs:
        bins[min(int(conf * ECE_BINS), ECE_BINS - 1)].append((conf, outcome))
    total = 0.0
    for members in bins.values():
        mean_conf = sum(conf for conf, _ in members) / len(members)
        mean_outcome = sum(outcome for _, outcome in members) / len(members)
        total += len(members) / len(pairs) * abs(mean_conf - mean_outcome)
    return round(total, 4)


def _rate(rows: list[dict], key: str) -> dict:
    n = len(rows)
    hits = sum(1 for row in rows if row[key] == 1)
    return {"k": hits, "n": n, "rate": round(hits / n, 4) if n else None, "ci95": wilson_interval(hits, n)}


def _condition_summary(rows: list[dict]) -> dict:
    n = len(rows)
    calibration = [
        (float(row["confidence"]), 1 if row["correct"] == 1 else 0)
        for row in rows
        if row["confidence"] is not None
    ]
    strict_rows = [{**row, "strict": int(row["correct"] == 1 and not row["malformed"])} for row in rows]
    recovery_rows = [row for row in rows if row["recovery_valid"] is not None]
    labels = Counter(label for row in rows for label in json.loads(row["failure_labels"] or "[]"))
    return {
        "accuracy": _rate(rows, "correct"),
        "strict": _rate(strict_rows, "strict"),
        "meets_proposal": _rate(rows, "meets_proposal"),
        "grounded": _rate(rows, "grounded"),
        "recovery": _rate(recovery_rows, "recovery_valid"),
        "invalid_after_correct": sum(1 for row in recovery_rows if row["correct"] == 1 and row["recovery_valid"] == 0),
        "failure_labels": dict(labels.most_common()),
        "malformed": sum(row["malformed"] for row in rows),
        "brier": brier_score(calibration),
        "ece": expected_calibration_error(calibration),
        "mean_llm_calls": round(sum(row["llm_call_count"] or 1 for row in rows) / n, 2) if n else None,
        "mean_input_tokens": round(sum(row["input_tokens"] or 0 for row in rows) / n, 1) if n else None,
        "mean_output_tokens": round(sum(row["output_tokens"] or 0 for row in rows) / n, 1) if n else None,
    }


def _paired(pairs: list[tuple[dict, dict]], key: str) -> dict:
    pairs = [(s, p) for s, p in pairs if s[key] is not None and p[key] is not None]
    only_single = sum(1 for s, p in pairs if s[key] == 1 and p[key] != 1)
    only_pipeline = sum(1 for s, p in pairs if s[key] != 1 and p[key] == 1)
    return {
        "only_single": only_single,
        "only_pipeline": only_pipeline,
        "p": round(exact_mcnemar(only_single, only_pipeline), 4),
    }


def analyze(conn: sqlite3.Connection, sweep_ids: list[str]) -> dict:
    """One result per (provider, model, request_count) across the given sweeps."""
    if not sweep_ids:
        return {"sweep_ids": [], "arms": []}

    marks = ",".join("?" for _ in sweep_ids)
    cursor = conn.execute(
        f"""
        SELECT sweep_id, provider, model, request_count, log_name, condition, true_fault, correct,
               malformed, confidence, meets_proposal, grounded, input_tokens, output_tokens,
               recovery_valid, failure_labels, llm_call_count
        FROM runs
        WHERE sweep_id IN ({marks}) AND api_error = 0
        ORDER BY id
        """,
        sweep_ids,
    )
    columns = [item[0] for item in cursor.description]

    groups: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for record in cursor.fetchall():
        row = dict(zip(columns, record))
        groups[(row["provider"], row["model"], row["request_count"])][row["condition"]].append(row)

    arms = []
    for (provider, model, request_count), conditions in sorted(groups.items(), key=lambda kv: str(kv[0])):
        single, pipeline = conditions.get("single_agent", []), conditions.get("pipeline", [])
        by_log = {(row["sweep_id"], row["log_name"]): row for row in pipeline}
        pairs = [
            (row, by_log[(row["sweep_id"], row["log_name"])])
            for row in single
            if (row["sweep_id"], row["log_name"]) in by_log
        ]
        others = {}
        for name, rows in conditions.items():
            if name in ("single_agent", "pipeline"):
                continue
            own = {(row["sweep_id"], row["log_name"]): row for row in rows}
            matched = [(row, own[(row["sweep_id"], row["log_name"])]) for row in single if (row["sweep_id"], row["log_name"]) in own]
            others[name] = {"summary": _condition_summary(rows), "n_pairs": len(matched), "vs_single": _paired(matched, "correct")}

        arms.append(
            {
                "provider": provider,
                "model": model,
                "request_count": request_count,
                "n_pairs": len(pairs),
                "single_agent": _condition_summary(single),
                "pipeline": _condition_summary(pipeline),
                "others": others,
                "paired": {key: _paired(pairs, key) for key in ("correct", "meets_proposal", "grounded", "recovery_valid")},
            }
        )
    return {"sweep_ids": sweep_ids, "arms": arms}
