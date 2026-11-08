"""The two experimental conditions, as streaming pipelines.

    single_agent : raw log text -> [Hypothesis Agent (LLM)] -> verdict
    pipeline     : raw log text -> [Log Correlator (deterministic)]
                                -> [Hypothesis Agent (LLM)]
                                -> [Runbook Agent (deterministic)] -> verdict

Both make exactly one LLM call. Both run the Runbook lookup, so the verdicts
are shaped identically and the only thing that differs is what the model was
shown. Each condition is written as a generator of trace events so the
dashboard can watch the agents fire in real time; `diagnose()` drains it for
callers that just want the answer.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Iterator

from .agents import runbook_agent
from .agents.hypothesis_agent import raw_log_payload, run_hypothesis, summary_payload, without_spacing
from .agents.log_correlator import correlate
from .agents.tool_agent import run_tool_agent
from .evidence import EvidenceReport, score_evidence
from .providers.base import LLMClient

CONDITIONS = ("single_agent", "pipeline")
EXTRA_CONDITIONS = ("pipeline_no_spacing", "tool_agent")
ALL_CONDITIONS = CONDITIONS + EXTRA_CONDITIONS
SUMMARY_CONDITIONS = ("pipeline", "pipeline_no_spacing")

CONDITION_LABELS = {
    "single_agent": "Single-agent baseline",
    "pipeline": "Multi-agent pipeline",
    "pipeline_no_spacing": "Pipeline without spacing fields",
    "tool_agent": "Tool-using agent",
}


@dataclass
class Diagnosis:
    condition: str
    provider: str
    model: str
    predicted_fault: str | None
    explanation: str
    confidence: float | None
    remediation: str
    ranked_alternatives: list[dict] = field(default_factory=list)
    agent_call_log: list[str] = field(default_factory=list)
    llm_call_count: int = 1
    malformed: bool = False
    api_error: bool = False
    parse_error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    llm_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    log_summary: dict | None = None
    evidence: dict | None = None
    recovery_requested: bool = False
    recovery: dict | None = None
    prompt_hint: bool = True
    compound: bool = False
    tool_trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _finalize(
    condition: str,
    client: LLMClient,
    hypothesis,
    log_text: str,
    summary,
    agent_call_log: list[str],
    started: float,
) -> Diagnosis:
    response = hypothesis.response
    usable = not (hypothesis.malformed or hypothesis.api_error)
    evidence = score_evidence(hypothesis.explanation, log_text, summary) if usable else EvidenceReport()

    return Diagnosis(
        condition=condition,
        provider=client.provider,
        model=client.model,
        predicted_fault=hypothesis.predicted_fault,
        explanation=hypothesis.explanation,
        confidence=hypothesis.confidence,
        remediation=runbook_agent.lookup(hypothesis.predicted_fault),
        ranked_alternatives=hypothesis.ranked_alternatives,
        agent_call_log=agent_call_log,
        llm_call_count=1,
        malformed=hypothesis.malformed,
        api_error=hypothesis.api_error,
        parse_error=hypothesis.parse_error,
        input_tokens=response.input_tokens if response else 0,
        output_tokens=response.output_tokens if response else 0,
        llm_latency_ms=round(response.latency_ms, 1) if response else 0.0,
        total_latency_ms=round((time.perf_counter() - started) * 1000, 1),
        log_summary=summary.to_dict(),
        evidence=evidence.to_dict(),
    )


def diagnose_stream(
    log_text: str,
    client: LLMClient,
    condition: str,
    recovery: bool = False,
    hint: bool = True,
    compound: bool = False,
) -> Iterator[dict]:
    """Run one condition, yielding a trace event per agent step, then the result."""
    if condition not in ALL_CONDITIONS:
        raise ValueError(f"unknown condition '{condition}'")

    started = time.perf_counter()
    agent_call_log: list[str] = []

    yield {
        "type": "run_start",
        "condition": condition,
        "label": CONDITION_LABELS[condition],
        "provider": client.provider,
        "model": client.model,
    }

    # The summary is always computed: in the pipeline it is the model's input,
    # in the single-agent arm it is used only to score the explanation's
    # evidence afterwards. It is never shown to the single-agent model.
    summary = correlate(log_text)

    llm_calls = 1
    tool_trace: list[dict] = []

    if condition == "tool_agent":
        hypothesis = None
        agent_call_log.append("tool_agent (LLM with read-only tools)")
        for event in run_tool_agent(log_text, client, recovery=recovery, hint=hint, compound=compound):
            if event["type"] == "agent_done":
                hypothesis, llm_calls, tool_trace = event["hypothesis"], event["llm_calls"], event["tool_trace"]
            else:
                yield event
    else:
        if condition in SUMMARY_CONDITIONS:
            yield {"type": "step_start", "agent": "log_correlator", "kind": "deterministic"}
            agent_call_log.append("log_correlator (deterministic)")
            shown = summary.to_dict()
            if condition == "pipeline_no_spacing":
                shown = without_spacing(shown)
            yield {
                "type": "step_done",
                "agent": "log_correlator",
                "kind": "deterministic",
                "output": shown,
            }
            payload = summary_payload(shown)
        else:
            payload = raw_log_payload(log_text)

        yield {
            "type": "step_start",
            "agent": "hypothesis_agent",
            "kind": "llm",
            "input_preview": payload[:600],
        }
        agent_call_log.append("hypothesis_agent (LLM)")

        hypothesis = run_hypothesis(client, payload, recovery=recovery, hint=hint, compound=compound)

        yield {
            "type": "step_done",
            "agent": "hypothesis_agent",
            "kind": "llm",
            "output": hypothesis.to_dict(),
        }

    yield {"type": "step_start", "agent": "runbook_agent", "kind": "deterministic"}
    agent_call_log.append("runbook_agent (deterministic)")
    remediation = runbook_agent.lookup(hypothesis.predicted_fault)
    yield {
        "type": "step_done",
        "agent": "runbook_agent",
        "kind": "deterministic",
        "output": {"remediation": remediation},
    }

    result = _finalize(condition, client, hypothesis, log_text, summary, agent_call_log, started)
    result.recovery_requested = recovery
    result.recovery = hypothesis.recovery
    result.llm_call_count = llm_calls
    result.prompt_hint = hint
    result.compound = compound
    result.tool_trace = tool_trace
    yield {"type": "result", "result": result.to_dict()}


def diagnose(
    log_text: str,
    client: LLMClient,
    condition: str,
    recovery: bool = False,
    hint: bool = True,
    compound: bool = False,
) -> Diagnosis:
    """Non-streaming convenience wrapper. Returns the final Diagnosis."""
    final: dict | None = None
    for event in diagnose_stream(log_text, client, condition, recovery=recovery, hint=hint, compound=compound):
        if event["type"] == "result":
            final = event["result"]
    if final is None:  # pragma: no cover - diagnose_stream always ends in a result
        raise RuntimeError("diagnosis produced no result")
    return Diagnosis(**final)
