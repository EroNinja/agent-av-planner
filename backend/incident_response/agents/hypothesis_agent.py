"""Hypothesis Agent -- the one and only LLM call in either condition.

Both experimental arms call this exact function with the exact same system
prompt, the same effort setting and the same token ceiling. The *only*
difference between them is the `user` payload: raw log text, or the Log
Correlator's structured summary. That single substitution is the independent
variable of the whole study, which is why nothing else may differ.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..providers.base import LLMClient, LLMResponse, extract_json
from ..taxonomy import FAULT_DESCRIPTIONS, FAULT_TYPES, RECOVERY_ACTIONS, RECOVERY_TARGETS

# Kept at the value used for the first evaluation sweep, so results stay comparable.
MAX_TOKENS = 2048

_LABEL_RE = re.compile(r'"predicted_fault"\s*:\s*"([^"]+)"')

SYSTEM_PROMPT = """You are an incident diagnosis agent. You are given observability \
data from a single run of a small service. Exactly one of the following conditions \
produced it, and you must decide which.

Candidate fault types:
{taxonomy}

Rules:
- Choose exactly one `predicted_fault` from: {labels}.
- Cite concrete evidence that actually appears in the data you were given: request \
ids, error type names, counts, or latency figures. Never invent a value you cannot \
point to in the input.
- Distinguish carefully between failures that are SPREAD ACROSS the run and failures \
that are CLUSTERED into a contiguous block of consecutive request ids. Both can \
produce the same error count; the distribution is what separates them.
- A healthy run may still contain isolated unrelated warnings or a one-off transient \
error. Those are not an injected fault. Report `no_fault` rather than inventing one.
- Baseline latency for a healthy run of this service is roughly 45ms.
- `confidence` is your genuine posterior that `predicted_fault` is correct, in [0, 1].

Respond with a single JSON object and nothing else -- no prose before or after, no \
markdown code fences:

{{
  "predicted_fault": "<one of {labels}>",
  "explanation": "<2-4 sentences citing specific evidence from the input>",
  "confidence": <float 0-1>,
  "ranked_alternatives": [
    {{"fault_type": "<label>", "confidence": <float>}}
  ]
}}

`ranked_alternatives` must list all {n} candidate labels in descending confidence."""


def _recovery_instructions() -> str:
    actions = "\n".join(f"- {name}: {text}" for name, text in RECOVERY_ACTIONS.items())
    targets = "\n".join(f"- {name}: {text}" for name, text in RECOVERY_TARGETS.items())
    return (
        "\n\nAlso add a `recovery` field to the JSON object: "
        '{"action": "<one action>", "target": "<one target>"}. '
        "Choose the single step most likely to restore the service.\n\n"
        f"Actions:\n{actions}\n\nTargets:\n{targets}"
    )


def system_prompt(recovery: bool = False) -> str:
    taxonomy = "\n".join(f"- {name}: {desc}" for name, desc in FAULT_DESCRIPTIONS.items())
    base = SYSTEM_PROMPT.format(
        taxonomy=taxonomy,
        labels=", ".join(FAULT_TYPES),
        n=len(FAULT_TYPES),
    )
    return base + _recovery_instructions() if recovery else base


def raw_log_payload(log_text: str) -> str:
    return (
        "Below is the complete raw log captured during one run of the service.\n\n"
        "----- BEGIN LOG -----\n"
        f"{log_text.strip()}\n"
        "----- END LOG -----\n\n"
        "Classify which fault type produced this run."
    )


def summary_payload(summary: dict) -> str:
    return (
        "Below is a structured summary of one run of the service, produced by a "
        "deterministic log parser. You are not shown the raw log.\n\n"
        "----- BEGIN SUMMARY -----\n"
        f"{json.dumps(summary, indent=2)}\n"
        "----- END SUMMARY -----\n\n"
        "Field notes: `error_request_ids` lists which requests failed. "
        "`error_id_spacing.gaps` gives the distance between consecutive failing ids and "
        "`error_id_spacing.max_contiguous_run` the longest unbroken block of them.\n\n"
        "Classify which fault type produced this run."
    )


@dataclass
class Hypothesis:
    predicted_fault: str | None
    explanation: str
    confidence: float | None
    ranked_alternatives: list[dict] = field(default_factory=list)
    malformed: bool = False
    api_error: bool = False
    parse_error: str | None = None
    response: LLMResponse | None = None
    recovery: dict | None = None

    def to_dict(self) -> dict:
        return {
            "predicted_fault": self.predicted_fault,
            "explanation": self.explanation,
            "confidence": self.confidence,
            "ranked_alternatives": self.ranked_alternatives,
            "malformed": self.malformed,
            "api_error": self.api_error,
            "parse_error": self.parse_error,
            "recovery": self.recovery,
        }


def _coerce_confidence(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _recover_label(text: str) -> str | None:
    """Pull the fault label out of a response whose JSON is otherwise broken.

    Formatting and diagnosis are separate abilities. A model that names the
    right fault and then writes `"confidence": 0. nine` diagnosed correctly and
    formatted badly, so the label is kept and `malformed` stays set. Strict
    accuracy still counts it as a miss.
    """
    match = _LABEL_RE.search(text or "")
    if not match:
        return None
    label = match.group(1).strip()
    return label if label in FAULT_TYPES else None


def _extract_recovery(parsed: dict) -> dict | None:
    plan = parsed.get("recovery")
    if not isinstance(plan, dict):
        return None
    return {"action": str(plan.get("action", "")).strip(), "target": str(plan.get("target", "")).strip()}


def run_hypothesis(client: LLMClient, payload: str, recovery: bool = False) -> Hypothesis:
    """Make the single model call and parse its answer tolerantly."""
    response = client.complete(system_prompt(recovery), payload, max_tokens=MAX_TOKENS)

    if not response.ok:
        return Hypothesis(
            predicted_fault=None,
            explanation="",
            confidence=None,
            api_error=True,
            parse_error=response.error,
            response=response,
        )

    parsed, error = extract_json(response.text)
    if parsed is None:
        return Hypothesis(
            predicted_fault=_recover_label(response.text),
            explanation=response.text[:500],
            confidence=None,
            malformed=True,
            parse_error=error,
            response=response,
        )

    predicted = parsed.get("predicted_fault")
    if isinstance(predicted, str):
        predicted = predicted.strip()
    if predicted not in FAULT_TYPES:
        return Hypothesis(
            predicted_fault=None,
            explanation=str(parsed.get("explanation", ""))[:1000],
            confidence=_coerce_confidence(parsed.get("confidence")),
            ranked_alternatives=parsed.get("ranked_alternatives") or [],
            malformed=True,
            parse_error=f"predicted_fault {predicted!r} is not in the taxonomy",
            response=response,
        )

    alternatives = parsed.get("ranked_alternatives")
    if not isinstance(alternatives, list):
        alternatives = []

    return Hypothesis(
        predicted_fault=predicted,
        explanation=str(parsed.get("explanation", ""))[:1000],
        confidence=_coerce_confidence(parsed.get("confidence")),
        ranked_alternatives=alternatives,
        response=response,
        recovery=_extract_recovery(parsed),
    )
