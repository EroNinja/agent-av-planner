"""A deterministic, offline stand-in for a model.

This exists so the backend, the dashboard, and the evaluation harness can be
developed and tested with no API key and no network. It is a rule engine, not a
model: it pattern-matches the text it is handed.

It pattern-matches strings rather than reasoning, so it is reported only as a
rules-only baseline, never as a model. The API rejects sweeps that mix mock with real models and leaves
mock out of the pooled stats view. A mock-only sweep can still be inspected on
its own, which is what offline development needs.
"""

from __future__ import annotations

import json
import re
import time

from ..agents.log_correlator import _longest_contiguous_run
from ..taxonomy import VALID_RECOVERY
from .base import LLMResponse

PROVIDER = "mock"
DEFAULT_MODEL = "mock-heuristic"
MODELS = [DEFAULT_MODEL]

_LATENCY_RE = re.compile(r"latency_ms(?:_mean)?[=\":\s]+([\d.]+)")
_IDS_RE = re.compile(r"error_request_ids\"?\s*:?\s*\[([^\]]*)\]")
_RAW_GENERIC_RE = re.compile(r"request_id=(\d+) status=error latency_ms=[\d.]+ error_type=OperationalError")


def _generic_error_ids(text: str) -> list[int]:
    raw = [int(value) for value in _RAW_GENERIC_RE.findall(text)]
    if raw:
        return raw
    match = _IDS_RE.search(text)
    if not match:
        return []
    return [int(token) for token in match.group(1).split(",") if token.strip().isdigit()]


def _mean_latency(text: str) -> float:
    values = [float(v) for v in _LATENCY_RE.findall(text)]
    return sum(values) / len(values) if values else 0.0


def _classify(text: str) -> tuple[str, float, str]:
    pool_hits = text.count("PoolExhaustedError")
    refused_hits = text.count("ConnectionRefusedError")
    mean_latency = _mean_latency(text)

    if "OperationalError" in text:
        ids = _generic_error_ids(text)
        if _longest_contiguous_run(ids) >= 4:
            return (
                "pool_exhausted",
                0.8,
                f"{len(ids)} OperationalError failures fall in a contiguous block of request ids.",
            )
        return (
            "dependency_down",
            0.8,
            f"{len(ids)} OperationalError failures are spread across the run, not clustered.",
        )

    if pool_hits and pool_hits >= refused_hits:
        return (
            "pool_exhausted",
            0.93,
            f"Found {pool_hits} PoolExhaustedError occurrences clustered over consecutive "
            "request ids, with latency on surviving requests at baseline.",
        )
    if refused_hits:
        return (
            "dependency_down",
            0.91,
            f"Found {refused_hits} ConnectionRefusedError occurrences spread across the run "
            "rather than clustered, with no latency growth.",
        )
    if mean_latency > 150:
        return (
            "slow_query",
            0.78,
            f"No error lines present, but latency averages {mean_latency:.0f}ms, far above "
            "the ~45ms baseline.",
        )
    return (
        "no_fault",
        0.9,
        "No error lines and latency at baseline; nothing indicates an injected fault.",
    )


class MockClient:
    provider = PROVIDER

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None):
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResponse:
        started = time.perf_counter()
        fault, confidence, explanation = _classify(user)

        others = [f for f in ("dependency_down", "pool_exhausted", "slow_query", "no_fault") if f != fault]
        remainder = round(1.0 - confidence, 4)
        ranked = [{"fault_type": fault, "confidence": confidence}] + [
            {"fault_type": name, "confidence": round(remainder / len(others), 4)} for name in others
        ]

        payload = {
            "predicted_fault": fault,
            "explanation": explanation,
            "confidence": confidence,
            "ranked_alternatives": ranked,
            "recovery": {"action": VALID_RECOVERY[fault][0], "target": VALID_RECOVERY[fault][1]},
        }

        return LLMResponse(
            text=json.dumps(payload, indent=2),
            provider=self.provider,
            model=self.model,
            input_tokens=len(user) // 4,
            output_tokens=len(json.dumps(payload)) // 4,
            latency_ms=(time.perf_counter() - started) * 1000,
            raw_meta={"synthetic": True},
        )
