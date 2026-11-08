"""Simulated recovery: would the action the model chose actually fix the run?

A correct diagnosis does not guarantee a correct fix. In recovery mode the model
also proposes one action and one target, inside its single call. This module
applies that plan to the toy service, replays the same scenario, and reads the
new log to see whether the service is healthy.

Nothing real is executed. The model never sees this result or the ground truth.
"""

from __future__ import annotations

from .agents.log_correlator import correlate
from .taxonomy import (
    RECOVERY_ACTIONS,
    RECOVERY_TARGETS,
    TRANSIENT_SIGNATURES,
    VALID_RECOVERY,
)
from .toy_service import run_service

HEALTHY_LATENCY_LIMIT_MS = 100.0
_TRANSIENT_TYPES = {error_type for error_type, _ in TRANSIENT_SIGNATURES}


def is_healthy(log_text: str) -> bool:
    """Healthy means no fault-class errors and normal latency, read from the log."""
    summary = correlate(log_text)
    fault_errors = [name for name in summary.error_type_counts if name not in _TRANSIENT_TYPES]
    return not fault_errors and summary.latency_ms_mean < HEALTHY_LATENCY_LIMIT_MS


def replay(meta: dict, action: str | None, target: str | None) -> dict:
    """Apply a plan to the scenario in `meta` and report the outcome."""
    fault = meta["fault_type"]
    result = {"action": action, "target": target, "valid": False, "healthy_after": False}

    if action not in RECOVERY_ACTIONS or target not in RECOVERY_TARGETS:
        return {**result, "reason": "the plan is not in the allowed actions and targets"}

    cured = VALID_RECOVERY[fault] == (action, target)
    remaining = "no_fault" if cured or fault == "no_fault" else fault
    after = run_service(
        remaining,
        run_id=int(meta["run_id"]),
        request_count=int(meta["request_count"]),
        noise=bool(meta.get("noise")),
    )
    healthy = is_healthy(after.render())

    if fault == "no_fault" and not cured:
        reason = "the service was already healthy, so this change was unnecessary"
    elif cured:
        reason = "the service was healthy after replay"
    else:
        reason = "the fault persisted after replay"
    return {**result, "valid": cured and healthy, "healthy_after": healthy, "reason": reason}


def assess_recovery(diagnosis: dict, meta: dict | None) -> dict | None:
    """Replay the diagnosis's plan. None when recovery was not requested or answered."""
    if meta is None or not diagnosis.get("recovery_requested") or diagnosis.get("api_error"):
        return None
    if "+" in meta["fault_type"]:  # one action cannot cure two faults
        return None
    plan = diagnosis.get("recovery") or {}
    if not plan.get("action"):
        return {
            "action": None,
            "target": None,
            "valid": False,
            "healthy_after": False,
            "reason": "the answer contained no usable recovery plan",
        }
    return replay(meta, plan.get("action"), plan.get("target"))
