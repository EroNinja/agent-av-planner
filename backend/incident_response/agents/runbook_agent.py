"""Runbook Agent -- step 3 of the pipeline. A dictionary lookup.

Deliberately not a model call. Mapping a known fault label to a fixed
remediation string is retrieval, not reasoning, and spending a second LLM call
on it would break the call-count parity that makes the single-agent vs pipeline
comparison a test of architecture rather than of API budget.
"""

from __future__ import annotations

from ..taxonomy import remediation_for


def lookup(fault_type: str | None) -> str:
    if not fault_type:
        return "No remediation available: the fault type could not be determined."
    return remediation_for(fault_type)
