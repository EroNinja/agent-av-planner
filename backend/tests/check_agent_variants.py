"""Offline checks for parser fixes, experiment variants, compound faults and the tool agent.

Run from backend/. No network.
"""

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, ".")

from incident_response import db
from incident_response.agents.hypothesis_agent import parse_answer, summary_payload, system_prompt, without_spacing
from incident_response.agents.log_correlator import correlate
from incident_response.agents.tool_agent import MAX_TOOL_ROUNDS, LogTools
from incident_response.analysis import analyze
from incident_response.conditions import diagnose, diagnose_stream
from incident_response.providers.base import LLMResponse, ToolCall, ToolTurn
from incident_response.providers.mock_client import MockClient
from incident_response.taxonomy import COMPOUND_FAULTS, remediation_for
from incident_response.toy_service import run_service

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        fails.append(name)


# parser fixes
noisy = run_service("pool_exhausted", 1, noise=True)
s = correlate(noisy.render())
check("spacing uses the dominant error type only", s.dominant_error_request_ids == noisy.injected_request_ids, s.dominant_error_request_ids)
check("transient beside a burst does not extend it", s.error_id_spacing["max_contiguous_run"] == len(noisy.injected_request_ids), s.error_id_spacing)
check("transients are listed separately", {e["error_type"] for e in s.isolated_errors} == {"TimeoutError"}, s.isolated_errors)
check("a real burst is not only isolated errors", s.only_isolated_errors is False)
for run in range(1, 11):
    q = run_service("no_fault", run, noise=True)
    q_s = correlate(q.render())
    if q.red_herring_request_ids:
        check(f"no_fault run {run} is flagged as isolated", q_s.only_isolated_errors is True)
    else:
        check(f"no_fault run {run} has no errors", q_s.error_count == 0 and q_s.only_isolated_errors is False)
clean = correlate(run_service("no_fault", 1).render())
check("no errors means no dominant type", clean.dominant_error_type is None and clean.error_id_spacing["max_contiguous_run"] == 0)

# variants
full = s.to_dict()
stripped = without_spacing(full)
check("no-spacing drops every derived field", not {"error_id_spacing", "dominant_error_type", "isolated_errors", "only_isolated_errors"} & stripped.keys())
check("no-spacing keeps the proposal's fields", "error_request_ids" in stripped and "latency_ms_p90" in stripped)
check("no-spacing payload does not describe missing fields", "error_id_spacing" not in summary_payload(stripped))
check("full payload describes its fields", "max_contiguous_run" in summary_payload(full))
check("hint is present by default", "SPREAD ACROSS" in system_prompt())
check("hint can be removed", "SPREAD ACROSS" not in system_prompt(hint=False))
check("no stray placeholder in the prompt", "{" not in system_prompt(hint=False).split("Respond with")[0])
check("compound prompt only when asked", "additional_faults" not in system_prompt() and "additional_faults" in system_prompt(compound=True))


class Fake:
    provider = "groq"
    model = "fake"

    def __init__(self, turns):
        self.turns = list(turns)
        self.tools_offered = []
        self.messages = None

    def complete_with_tools(self, system, messages, tools, max_tokens=2048):
        self.tools_offered.append(bool(tools))
        self.messages = messages
        return self.turns.pop(0)


log = run_service("pool_exhausted", 1).render()
tools = LogTools(log)
check("overview tool", tools.call("get_overview", {})["total_requests"] == 30)
check("list_errors filters by type", tools.call("list_errors", {"error_type": "PoolExhaustedError"})["error_request_ids"] == [12, 13, 14, 15, 16])
check("unknown error type is empty", tools.call("list_errors", {"error_type": "Nope"})["error_request_ids"] == [])
check("measure_spacing", tools.call("measure_spacing", {})["max_contiguous_run"] == 5)
check("read_lines caps its range", len(tools.call("read_lines", {"start_request_id": 0, "count": 500})["lines"]) == 10)
check("read_lines rejects bad arguments", "error" in tools.call("read_lines", {"start_request_id": "x"}))
check("unknown tool is reported", "error" in tools.call("nope", {}))

final = json.dumps({"predicted_fault": "pool_exhausted", "explanation": "Errors at request_id=12,13.", "confidence": 0.9})
call = ToolCall(id="1", name="list_errors", arguments={})
fake = Fake([ToolTurn(text="", tool_calls=[call], input_tokens=10, output_tokens=5), ToolTurn(text=final, input_tokens=20, output_tokens=7)])
events = list(diagnose_stream(log, fake, "tool_agent"))
result = events[-1]["result"]
check("agent diagnoses through its tools", result["predicted_fault"] == "pool_exhausted" and result["llm_call_count"] == 2, result["llm_call_count"])
check("agent tokens are summed", result["input_tokens"] == 30 and result["output_tokens"] == 12)
check("tool steps appear in the trace", any(e.get("kind") == "tool" for e in events))
check("tool result goes back to the model", any(m["role"] == "tool" for m in fake.messages))
check("tool calls are recorded", result["tool_trace"][0]["tool"] == "list_errors")

bad = json.dumps({"predicted_fault": "pool_exhausted", "explanation": "Errors at request_id=2,3.", "confidence": 0.9})
fake = Fake([ToolTurn(text=bad), ToolTurn(text=final)])
events = list(diagnose_stream(log, fake, "tool_agent"))
result = events[-1]["result"]
check("unsupported claims trigger one revision", result["llm_call_count"] == 2 and result["evidence"]["score"] == 1.0, result["evidence"])
check("revision is in the trace", any(str(e.get("agent", "")).startswith("self_check") for e in events))
fake = Fake([ToolTurn(text=bad), ToolTurn(text=bad)])
result = list(diagnose_stream(log, fake, "tool_agent"))[-1]["result"]
check("revision happens only once", result["llm_call_count"] == 2)

loop_call = [ToolTurn(text="", tool_calls=[ToolCall(id=str(i), name="get_overview", arguments={})]) for i in range(MAX_TOOL_ROUNDS)]
fake = Fake([*loop_call, ToolTurn(text=final)])
result = list(diagnose_stream(log, fake, "tool_agent"))[-1]["result"]
check("tool budget is enforced", fake.tools_offered[-1] is False and fake.tools_offered.count(True) == MAX_TOOL_ROUNDS and result["predicted_fault"] == "pool_exhausted")
fake = Fake([ToolTurn(text="", error="RateLimitError: 429 on tokens per day (TPD)")])
result = list(diagnose_stream(log, fake, "tool_agent"))[-1]["result"]
check("agent failure is an API error", result["api_error"] is True and result["llm_call_count"] == 1)

for condition in ("tool_agent", "pipeline_no_spacing"):
    r = diagnose(log, MockClient(), condition)
    check(f"{condition} works on the mock", r.predicted_fault == "pool_exhausted", r.predicted_fault)

# compound
for fault in COMPOUND_FAULTS:
    run = run_service(fault, 1)
    cs = correlate(run.render())
    check(f"{fault} has errors and high latency", cs.error_count > 0 and cs.latency_ms_mean > 150, (cs.error_count, cs.latency_ms_mean))
    check(f"{fault} is deterministic", run.render() == run_service(fault, 1).render())
    r = diagnose(run.render(), MockClient(), "pipeline", compound=True)
    check(f"{fault} scored as an exact set", r.predicted_fault == fault, r.predicted_fault)
check("single-fault logs unchanged by compound support", run_service("pool_exhausted", 1).injected_request_ids == [12, 13, 14, 15, 16])
extra = LLMResponse(text='{"predicted_fault":"slow_query","additional_faults":["pool_exhausted","bogus"],"confidence":0.5}', provider="x", model="y")
check("additional faults are combined and sorted", parse_answer(extra, compound=True).predicted_fault == "pool_exhausted+slow_query")
check("extras are ignored outside compound mode", parse_answer(extra).predicted_fault == "slow_query")
check("runbook covers both faults", "pool" in remediation_for("pool_exhausted+slow_query").lower() and "index" in remediation_for("pool_exhausted+slow_query").lower())

tmp = pathlib.Path(tempfile.mkdtemp())
conn = db.connect(tmp / "x.db")
db.init_db(conn)
run = run_service("pool_exhausted", 1)
for condition in ("single_agent", "pipeline", "tool_agent", "pipeline_no_spacing"):
    db.record_run(conn, diagnose(run.render(), MockClient(), condition).to_dict(), "a", "pool_exhausted", sweep_id="s", meta=run.meta())
arm = analyze(conn, ["s"])["arms"][0]
check("analysis reports extra conditions", set(arm["others"]) == {"tool_agent", "pipeline_no_spacing"})
check("analysis reports llm calls", arm["others"]["tool_agent"]["summary"]["mean_llm_calls"] == 3.0, arm["others"]["tool_agent"]["summary"]["mean_llm_calls"])

print("\nFAILED: " + ", ".join(fails) if fails else "\nALL AGENT CHECKS PASSED")
sys.exit(1 if fails else 0)
