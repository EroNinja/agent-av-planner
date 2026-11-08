"""Tool-using diagnosis agent.

Unlike the fixed pipeline, this agent chooses what to inspect. The model calls
read-only tools over the log, decides when it has enough evidence, and answers.
Before finishing, a deterministic check compares its citations with the log and
gives it one chance to correct anything unsupported.

It makes several LLM calls, so it is compared on tokens as well as accuracy.
"""

from __future__ import annotations

import json
import time
from typing import Iterator

from ..evidence import score_evidence
from ..providers.base import LLMResponse
from .hypothesis_agent import MAX_TOKENS, parse_answer, system_prompt
from .log_correlator import LINE_RE, correlate, gap_profile

MAX_TOOL_ROUNDS = 5
MAX_LINES = 10
RATE_LIMIT_WAITS = (5.0, 15.0, 30.0, 60.0, 60.0)

TOOLS = [
    {
        "name": "get_overview",
        "description": "Totals for the run: requests, successes, errors by type, warning count and latency statistics.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_errors",
        "description": "Request ids that failed, optionally limited to one error type.",
        "parameters": {
            "type": "object",
            "properties": {"error_type": {"type": "string", "description": "Optional error type name."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "measure_spacing",
        "description": "Gaps between failing request ids and the longest unbroken run, optionally for one error type.",
        "parameters": {
            "type": "object",
            "properties": {"error_type": {"type": "string", "description": "Optional error type name."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "read_lines",
        "description": f"Raw log lines for consecutive request ids, at most {MAX_LINES} requests.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_request_id": {"type": "integer"},
                "count": {"type": "integer", "description": f"Between 1 and {MAX_LINES}."},
            },
            "required": ["start_request_id"],
            "additionalProperties": False,
        },
    },
]

AGENT_INSTRUCTIONS = (
    "\n\nYou cannot see the log directly. Inspect it with the tools, choosing what to look at. "
    f"You have at most {MAX_TOOL_ROUNDS} rounds of tool calls. When you have enough evidence, "
    "stop calling tools and reply with the final JSON object."
)


class LogTools:
    """Read-only views of one log."""

    def __init__(self, log_text: str):
        self.summary = correlate(log_text)
        self.errors: dict[str, list[int]] = {}
        self.lines_by_id: dict[int, str] = {}
        for raw in log_text.splitlines():
            match = LINE_RE.match(raw.strip())
            if not match or match["request_id"] is None:
                continue
            request_id = int(match["request_id"])
            self.lines_by_id[request_id] = raw.strip()
            if match["status"] == "error":
                self.errors.setdefault(match["error_type"] or "UnknownError", []).append(request_id)

    def _ids(self, error_type: str | None) -> list[int]:
        if error_type:
            return sorted(self.errors.get(error_type, []))
        return sorted(rid for ids in self.errors.values() for rid in ids)

    def call(self, name: str, arguments: dict) -> dict:
        if name == "get_overview":
            s = self.summary
            return {
                "total_requests": s.total_requests,
                "ok_count": s.ok_count,
                "error_count": s.error_count,
                "error_type_counts": s.error_type_counts,
                "warning_count": s.warning_count,
                "latency_ms_mean": s.latency_ms_mean,
                "latency_ms_p90": s.latency_ms_p90,
                "latency_ms_max": s.latency_ms_max,
            }
        if name == "list_errors":
            error_type = arguments.get("error_type")
            return {"error_type": error_type or "all", "error_request_ids": self._ids(error_type)}
        if name == "measure_spacing":
            error_type = arguments.get("error_type")
            return {"error_type": error_type or "all", **gap_profile(self._ids(error_type))}
        if name == "read_lines":
            try:
                start = int(arguments.get("start_request_id"))
                count = max(1, min(MAX_LINES, int(arguments.get("count", MAX_LINES))))
            except (TypeError, ValueError):
                return {"error": "start_request_id and count must be integers"}
            return {"lines": [self.lines_by_id[rid] for rid in range(start, start + count) if rid in self.lines_by_id]}
        return {"error": f"unknown tool '{name}'"}


def _call_model(client, system: str, messages: list, tools: list):
    for wait in (*RATE_LIMIT_WAITS, None):
        turn = client.complete_with_tools(system, messages, tools, max_tokens=MAX_TOKENS)
        lowered = (turn.error or "").lower()
        if "429" not in lowered or "per day" in lowered or wait is None:
            return turn
        time.sleep(wait)
    return turn  # pragma: no cover


def run_tool_agent(
    log_text: str,
    client,
    recovery: bool = False,
    hint: bool = True,
    compound: bool = False,
) -> Iterator[dict]:
    """Yield a trace event per step, ending with an `agent_done` event."""
    tools = LogTools(log_text)
    system = system_prompt(recovery, hint, compound) + AGENT_INSTRUCTIONS
    messages: list[dict] = [{"role": "user", "content": "One run's log is available through the tools. Diagnose it."}]
    usage = {"input": 0, "output": 0, "latency": 0.0, "calls": 0}
    trace: list[dict] = []
    revised = False
    rounds = 0
    step = 0

    def finish(hypothesis) -> dict:
        return {"type": "agent_done", "hypothesis": hypothesis, "llm_calls": usage["calls"], "tool_trace": trace}

    def total(text: str, error: str | None = None) -> LLMResponse:
        return LLMResponse(
            text=text,
            provider=client.provider,
            model=client.model,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            latency_ms=usage["latency"],
            error=error,
        )

    while True:
        offered = TOOLS if rounds < MAX_TOOL_ROUNDS else []
        step += 1
        agent = f"agent_decision_{step}"
        yield {"type": "step_start", "agent": agent, "kind": "llm"}
        turn = _call_model(client, system, messages, offered)
        usage["calls"] += 1
        usage["input"] += turn.input_tokens
        usage["output"] += turn.output_tokens
        usage["latency"] += turn.latency_ms

        if turn.error:
            yield {"type": "step_done", "agent": agent, "kind": "llm", "output": {"error": turn.error}}
            yield finish(parse_answer(total("", turn.error), compound))
            return

        if turn.tool_calls and offered:
            names = [call.name for call in turn.tool_calls]
            yield {"type": "step_done", "agent": agent, "kind": "llm", "output": {"chose_tools": names}}
            rounds += 1
            messages.append(
                {"role": "assistant", "content": turn.text, "tool_calls": turn.tool_calls, "provider_state": turn.provider_state}
            )
            for call in turn.tool_calls:
                step += 1
                tool_agent_name = f"tool_{call.name}_{step}"
                yield {"type": "step_start", "agent": tool_agent_name, "kind": "tool"}
                result = tools.call(call.name, call.arguments)
                trace.append({"tool": call.name, "arguments": call.arguments, "result": result})
                yield {"type": "step_done", "agent": tool_agent_name, "kind": "tool", "output": result}
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(result)}
                )
            continue

        yield {"type": "step_done", "agent": agent, "kind": "llm", "output": {"answered": True}}
        hypothesis = parse_answer(total(turn.text), compound)

        check = score_evidence(hypothesis.explanation, log_text, tools.summary) if not hypothesis.malformed else None
        if check and check.unsupported and not revised:
            revised = True
            step += 1
            name = f"self_check_{step}"
            yield {"type": "step_start", "agent": name, "kind": "deterministic"}
            yield {"type": "step_done", "agent": name, "kind": "deterministic", "output": {"unsupported": check.unsupported}}
            trace.append({"tool": "self_check", "arguments": {}, "result": {"unsupported": check.unsupported}})
            messages.append({"role": "assistant", "content": turn.text, "provider_state": turn.provider_state})
            messages.append(
                {
                    "role": "user",
                    "content": "An automatic check could not confirm these claims in the log: "
                    + "; ".join(check.unsupported)
                    + ". Recheck them, then reply with the corrected final JSON object.",
                }
            )
            continue

        yield finish(hypothesis)
        return
