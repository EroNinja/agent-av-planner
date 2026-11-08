"""Log Correlator -- step 1 of the multi-agent pipeline. Fully deterministic.

Parses raw log text into a structured summary. No model call: parsing and
counting are arithmetic, not reasoning. This is the component whose value the
whole experiment is testing -- the pipeline hands the Hypothesis Agent *this*
instead of the raw text, at an identical LLM call budget.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

ISOLATED_ERROR_LIMIT = 2

LINE_RE = re.compile(
    r"^(?P<ts>\S+)\s+level=(?P<level>\w+)"
    r"(?:\s+component=(?P<component>\S+))?"
    r"(?:\s+request_id=(?P<request_id>\d+))?"
    r"(?:\s+status=(?P<status>\w+))?"
    r"(?:\s+latency_ms=(?P<latency>[\d.]+))?"
    r"(?:\s+error_type=(?P<error_type>\S+))?"
    r'(?:\s+message="(?P<message>[^"]*)")?'
    r"\s*$"
)


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Deterministic and dependency-free."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(pct / 100.0 * len(ordered))))
    return ordered[rank - 1]


def _longest_contiguous_run(ids: list[int]) -> int:
    """Length of the longest run of consecutive request ids."""
    if not ids:
        return 0
    ordered = sorted(set(ids))
    best = current = 1
    for prev, cur in zip(ordered, ordered[1:]):
        current = current + 1 if cur == prev + 1 else 1
        best = max(best, current)
    return best


def gap_profile(ids: list[int]) -> dict:
    """Spacing between failing request ids -- separates 'spread out' from 'clustered'.

    This is the single most load-bearing derived field in the summary: a
    contiguous burst (pool exhaustion) and an evenly spread cadence (dependency
    down) have identical error counts and identical latency, and differ only
    here.
    """
    ordered = sorted(set(ids))
    if len(ordered) < 2:
        return {"gaps": [], "mean_gap": None, "max_contiguous_run": _longest_contiguous_run(ids)}
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    return {
        "gaps": gaps,
        "mean_gap": round(sum(gaps) / len(gaps), 2),
        "max_contiguous_run": _longest_contiguous_run(ids),
    }


@dataclass
class LogSummary:
    total_requests: int = 0
    ok_count: int = 0
    error_count: int = 0
    error_type_counts: dict[str, int] = field(default_factory=dict)
    error_request_ids: list[int] = field(default_factory=list)
    sample_error_messages: list[str] = field(default_factory=list)
    latency_ms_mean: float = 0.0
    latency_ms_max: float = 0.0
    latency_ms_p90: float = 0.0
    dominant_error_type: str | None = None
    dominant_error_request_ids: list[int] = field(default_factory=list)
    error_id_spacing: dict = field(default_factory=dict)
    isolated_errors: list[dict] = field(default_factory=list)
    only_isolated_errors: bool = False
    warning_count: int = 0
    sample_warnings: list[str] = field(default_factory=list)
    unparsed_lines: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def correlate(log_text: str) -> LogSummary:
    """Parse raw log text into a structured summary. The pipeline's step 1."""
    summary = LogSummary()
    latencies: list[float] = []
    ids_by_type: dict[str, list[int]] = {}
    seen_messages: list[str] = []
    seen_warnings: list[str] = []

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = LINE_RE.match(line)
        if not match:
            summary.unparsed_lines += 1
            continue

        groups = match.groupdict()

        if groups["level"] == "WARN" or groups["request_id"] is None:
            summary.warning_count += 1
            message = groups.get("message")
            if message and message not in seen_warnings:
                seen_warnings.append(message)
            continue

        summary.total_requests += 1
        if groups["latency"] is not None:
            latencies.append(float(groups["latency"]))

        if groups["status"] == "error":
            summary.error_count += 1
            summary.error_request_ids.append(int(groups["request_id"]))
            error_type = groups["error_type"] or "UnknownError"
            summary.error_type_counts[error_type] = summary.error_type_counts.get(error_type, 0) + 1
            ids_by_type.setdefault(error_type, []).append(int(groups["request_id"]))
            message = groups.get("message")
            if message and message not in seen_messages:
                seen_messages.append(message)
        else:
            summary.ok_count += 1

    summary.sample_error_messages = seen_messages[:3]
    summary.sample_warnings = seen_warnings[:3]
    summary.error_request_ids.sort()
    # Spacing is measured on the most common error type, so that an unrelated
    # transient beside a burst cannot extend the burst.
    if ids_by_type:
        dominant = max(ids_by_type, key=lambda name: (len(ids_by_type[name]), name))
        summary.dominant_error_type = dominant
        summary.dominant_error_request_ids = sorted(ids_by_type[dominant])
        summary.only_isolated_errors = len(ids_by_type[dominant]) <= ISOLATED_ERROR_LIMIT
        summary.isolated_errors = [
            {"error_type": name, "request_ids": sorted(ids)}
            for name, ids in sorted(ids_by_type.items())
            if name != dominant
        ]
    summary.error_id_spacing = gap_profile(summary.dominant_error_request_ids)

    if latencies:
        summary.latency_ms_mean = round(sum(latencies) / len(latencies), 1)
        summary.latency_ms_max = round(max(latencies), 1)
        summary.latency_ms_p90 = round(_percentile(latencies, 90), 1)

    return summary
