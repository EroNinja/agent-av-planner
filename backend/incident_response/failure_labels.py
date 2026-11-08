"""Name what went wrong in a run, using deterministic rules only.

The idea of a reasoning-failure vocabulary comes from Riddell et al. (FORGE 2026),
but these labels are this project's own and cover only what the logs and ground
truth can confirm without another model call. A run can carry several labels, and
a run with none had no detectable problem.
"""

from __future__ import annotations

from .taxonomy import TRANSIENT_SIGNATURES

SPREAD_AND_BURST = {"dependency_down", "pool_exhausted"}


def _blamed_red_herring(diagnosis: dict, evidence: dict, meta: dict | None) -> bool:
    if not meta or not meta.get("red_herring_request_ids"):
        return False
    if set(meta["red_herring_request_ids"]) & set(evidence.get("cited_failure_ids") or []):
        return True
    explanation = diagnosis.get("explanation") or ""
    return any(error_type in explanation for error_type, _ in TRANSIENT_SIGNATURES)


def label_run(
    true_fault: str,
    diagnosis: dict,
    meta: dict | None,
    grounded: int | None,
    recovery: dict | None,
) -> list[str]:
    if diagnosis.get("api_error"):
        return ["call_failed"]

    labels: list[str] = []
    predicted = diagnosis.get("predicted_fault")
    evidence = diagnosis.get("evidence") or {}

    if diagnosis.get("parse_error") == "empty response":
        labels.append("empty_answer")
    elif diagnosis.get("malformed"):
        labels.append("malformed_output")

    if int(evidence.get("claims_checked") or 0) > int(evidence.get("claims_supported") or 0):
        labels.append("invented_evidence")

    if predicted and predicted != true_fault:
        if "+" in true_fault or "+" in predicted:
            labels.append("wrong_fault_set")
        elif true_fault == "no_fault":
            labels.append("false_alarm")
        elif predicted == "no_fault":
            labels.append("missed_fault")
        elif {true_fault, predicted} == SPREAD_AND_BURST:
            labels.append("confused_spread_and_burst")
        else:
            labels.append("misjudged_latency")
        if not diagnosis.get("malformed") and _blamed_red_herring(diagnosis, evidence, meta):
            labels.append("blamed_red_herring")

    if predicted == true_fault:
        if grounded == 0 and not diagnosis.get("malformed") and "invented_evidence" not in labels:
            labels.append("right_but_ungrounded")
        if recovery is not None and not recovery["valid"]:
            labels.append("invalid_recovery")

    return labels
