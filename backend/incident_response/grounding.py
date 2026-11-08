"""Does a diagnosis meet the proposal's success criterion, and is it grounded?

Two levels, both stricter than accuracy:

  meets_proposal : the fault is right and the explanation cites nothing that is
                   absent from the log (proposal section 1).
  grounded       : it also points at what actually caused the failure -- an
                   injected request id or the spacing of the failures, or for
                   slow_query a measured latency.
                   Right label plus unrelated evidence is right by luck.

Ground truth comes from the `.meta.json` sidecar, which no model ever sees.
"""

from __future__ import annotations


def _cites_cause(true_fault: str, evidence: dict, meta: dict) -> bool:
    if "+" in true_fault:
        return all(_cites_cause(part, evidence, meta) for part in true_fault.split("+"))
    if true_fault == "no_fault":
        return True
    if true_fault == "slow_query":
        return int(evidence.get("latency_supported") or 0) > 0
    if evidence.get("spacing_cited"):
        return True  # spread versus clustered is what separates these two faults
    injected = set(meta.get("injected_request_ids") or [])
    return bool(injected & set(evidence.get("cited_failure_ids") or []))


def assess(true_fault: str, diagnosis: dict, meta: dict | None) -> tuple[int | None, int | None]:
    """Return (meets_proposal, grounded). None means the call produced no answer."""
    if diagnosis.get("api_error"):
        return None, None
    if diagnosis.get("malformed"):
        return 0, 0  # an unreadable explanation cannot satisfy either level

    evidence = diagnosis.get("evidence") or {}
    fabricated = int(evidence.get("claims_checked") or 0) > int(evidence.get("claims_supported") or 0)
    meets = diagnosis.get("predicted_fault") == true_fault and not fabricated
    if meta is None:
        return int(meets), None
    return int(meets), int(meets and _cites_cause(true_fault, evidence, meta))
