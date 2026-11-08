"""Sweep engine: run every (log x model x condition) cell and score it.

A sweep is the experiment. One cell = one log, one provider/model, one
condition = exactly one LLM call. Results stream out as they land so the
dashboard can fill in live, and every cell is written to SQLite immediately --
a sweep that dies halfway still leaves usable data behind.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Iterator, Sequence

from .conditions import CONDITIONS, diagnose
from .db import record_run
from .log_generator import LogRecord
from .providers import PROVIDERS, get_client
from .providers.base import ProviderError

RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "too many requests")
MAX_ATTEMPTS = 7
BACKOFF_SECONDS = (5.0, 15.0, 30.0, 60.0, 60.0, 60.0)
MAX_CONSECUTIVE_FAILURES = 3
DAILY_CAP_MARKERS = ("per day", "(tpd)", "(rpd)")


@dataclass(frozen=True)
class Arm:
    """One experimental arm: a provider/model pair."""

    provider: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


def _looks_rate_limited(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def _diagnose_with_backoff(log_text: str, client, condition: str, recovery: bool = False):
    """Retry only on rate limits. Every other failure is recorded as data.

    Groq's free tier is generous but per-minute capped, and a long sweep will
    brush against it. A malformed answer, by contrast, is never retried -- the
    malformed rate is one of the metrics.
    """
    attempt = 0
    while True:
        result = diagnose(log_text, client, condition, recovery=recovery)
        if not (result.api_error and _looks_rate_limited(result.parse_error)):
            return result
        if _is_daily_cap(result.parse_error) or attempt >= MAX_ATTEMPTS - 1:
            return result
        time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
        attempt += 1


def _is_daily_cap(error: str | None) -> bool:
    """A daily quota will not clear within any backoff worth waiting for."""
    lowered = (error or "").lower()
    return any(marker in lowered for marker in DAILY_CAP_MARKERS)


def validate_arms(arms: Sequence[Arm]) -> None:
    """A sweep is either all real models, or all non-comparable ones."""
    comparable = {PROVIDERS[arm.provider]["comparable"] for arm in arms}
    if len(comparable) > 1:
        raise ValueError("mock cannot be mixed with real models in one sweep")


def balanced_order(records: Sequence[LogRecord]) -> list[LogRecord]:
    """Order logs round-robin by run number, one of each fault per round.

    Alphabetical order runs every dependency_down log before any slow_query
    log, so a sweep cut short by a quota would lose whole fault types. This way
    it loses whole rounds and stays balanced.
    """

    def key(record: LogRecord) -> tuple[int, str]:
        match = re.search(r"_run(\d+)", record.name)
        return (int(match.group(1)) if match else 0, record.name)

    return sorted(records, key=key)


def plan_size(n_logs: int, arms: Sequence[Arm], conditions: Sequence[str]) -> int:
    return n_logs * len(arms) * len(conditions)


def run_sweep_stream(
    records: Sequence[LogRecord],
    arms: Sequence[Arm],
    conn: sqlite3.Connection,
    conditions: Sequence[str] = CONDITIONS,
    sweep_id: str | None = None,
    recovery: bool = False,
) -> Iterator[dict]:
    """Execute a sweep, yielding one event per completed cell."""
    sweep_id = sweep_id or uuid.uuid4().hex[:12]
    total = plan_size(len(records), arms, conditions)
    completed = 0

    yield {
        "type": "sweep_start",
        "sweep_id": sweep_id,
        "total": total,
        "n_logs": len(records),
        "arms": [arm.key for arm in arms],
        "conditions": list(conditions),
    }

    for arm in arms:
        try:
            client = get_client(arm.provider, arm.model)
        except ProviderError as exc:
            completed += len(records) * len(conditions)
            yield {
                "type": "arm_failed",
                "sweep_id": sweep_id,
                "arm": arm.key,
                "error": str(exc),
                "completed": completed,
                "total": total,
            }
            continue

        yield {"type": "arm_start", "sweep_id": sweep_id, "arm": arm.key}

        arm_base = completed
        consecutive_failures = 0
        abort_reason: str | None = None

        for record in balanced_order(records):
            log_text = record.text()
            for condition in conditions:
                result = _diagnose_with_backoff(log_text, client, condition, recovery)
                payload = result.to_dict()
                run_id = record_run(
                    conn,
                    payload,
                    log_name=record.name,
                    true_fault=record.true_fault,
                    noise=record.noise,
                    sweep_id=sweep_id,
                    meta=record.meta(),
                )
                completed += 1
                consecutive_failures = consecutive_failures + 1 if result.api_error else 0
                correct = result.predicted_fault == record.true_fault
                yield {
                    "type": "cell",
                    "sweep_id": sweep_id,
                    "run_id": run_id,
                    "arm": arm.key,
                    "log_name": record.name,
                    "true_fault": record.true_fault,
                    "condition": condition,
                    "predicted_fault": result.predicted_fault,
                    "correct": correct,
                    "strict_correct": correct and not result.malformed,
                    "confidence": result.confidence,
                    "malformed": result.malformed,
                    "api_error": result.api_error,
                    "parse_error": result.parse_error,
                    "evidence": result.evidence,
                    "latency_ms": result.llm_latency_ms,
                    "completed": completed,
                    "total": total,
                }
                daily_cap = result.api_error and _is_daily_cap(result.parse_error)
                if daily_cap or consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    abort_reason = result.parse_error or "repeated API failures"
                    break
            if abort_reason:
                break

        if abort_reason:
            completed = arm_base + len(records) * len(conditions)
            yield {
                "type": "arm_failed",
                "sweep_id": sweep_id,
                "arm": arm.key,
                "error": f"stopped early: {abort_reason}",
                "completed": completed,
                "total": total,
            }
            continue

        yield {"type": "arm_done", "sweep_id": sweep_id, "arm": arm.key}

    yield {"type": "sweep_done", "sweep_id": sweep_id, "completed": completed, "total": total}


def summarize_cells(cells: Sequence[dict]) -> dict:
    """Aggregate in-memory sweep cells -- used by the CLI's console report."""
    summary: dict = {}
    for cell in cells:
        key = (cell["arm"], cell["condition"])
        bucket = summary.setdefault(
            key,
            {"n": 0, "correct": 0, "strict": 0, "malformed": 0, "failed": 0,
             "confidence_sum": 0.0, "confidence_n": 0, "by_fault": {}},
        )
        if cell.get("api_error"):
            bucket["failed"] += 1
            continue

        bucket["n"] += 1
        bucket["correct"] += int(bool(cell["correct"]))
        bucket["strict"] += int(bool(cell.get("strict_correct")))
        bucket["malformed"] += int(bool(cell["malformed"]))
        if cell.get("confidence") is not None:
            bucket["confidence_sum"] += float(cell["confidence"])
            bucket["confidence_n"] += 1

        fault_bucket = bucket["by_fault"].setdefault(cell["true_fault"], {"n": 0, "correct": 0})
        fault_bucket["n"] += 1
        fault_bucket["correct"] += int(bool(cell["correct"]))
    return summary
