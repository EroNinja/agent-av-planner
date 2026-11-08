"""Offline Batch 2 checks. Run from incident-response/backend. No network."""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, ".")

from incident_response import db
from incident_response.agents.hypothesis_agent import run_hypothesis, system_prompt
from incident_response.analysis import analyze
from incident_response.conditions import diagnose
from incident_response.failure_labels import label_run
from incident_response.providers.mock_client import MockClient
from incident_response.providers.base import LLMResponse
from incident_response.recovery import assess_recovery, is_healthy, replay
from incident_response.taxonomy import VALID_RECOVERY
from incident_response.toy_service import run_service

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        fails.append(name)


class Fake:
    provider = "groq"
    model = "fake"

    def __init__(self, text):
        self.text = text
        self.system = None

    def complete(self, system, user, max_tokens=4096):
        self.system = system
        return LLMResponse(text=self.text, provider="groq", model="fake")


def meta(fault, noise=False):
    return run_service(fault, 1, noise=noise).meta()


check("prompt unchanged when recovery is off", "recovery" not in system_prompt(False).lower())
check("prompt lists actions when on", "failover_dependency" in system_prompt(True) and "connection-pool" in system_prompt(True))

for noise in (False, True):
    for fault, plan in VALID_RECOVERY.items():
        r = replay(meta(fault, noise), *plan)
        check(f"correct plan fixes {fault} noise={noise}", r["valid"] and r["healthy_after"], r["reason"])
for fault in ("dependency_down", "pool_exhausted", "slow_query"):
    r = replay(meta(fault), "restart_service", "api-service")
    check(f"restart does not fix {fault}", not r["valid"] and not r["healthy_after"], r["reason"])
    r = replay(meta(fault), "no_action", "none")
    check(f"doing nothing leaves {fault}", not r["valid"] and not r["healthy_after"], r["reason"])
r = replay(meta("pool_exhausted"), "raise_pool_limit", "db-primary")
check("right action on wrong target is invalid", not r["valid"], r["reason"])
r = replay(meta("no_fault"), "restart_service", "api-service")
check("change to a healthy service is invalid but healthy", not r["valid"] and r["healthy_after"] and "unnecessary" in r["reason"], r["reason"])
r = replay(meta("slow_query"), "drop_table", "db-primary")
check("action outside the list is invalid", not r["valid"] and "allowed" in r["reason"], r["reason"])
check("noisy no_fault with transients is healthy", is_healthy(run_service("no_fault", 2, noise=True).render()))
check("fault log is not healthy", not is_healthy(run_service("pool_exhausted", 1).render()))
check("slow log is not healthy", not is_healthy(run_service("slow_query", 1).render()))

check("no result when not requested", assess_recovery({"recovery": {"action": "x"}}, meta("no_fault")) is None)
check("no result on failed call", assess_recovery({"recovery_requested": True, "api_error": True}, meta("no_fault")) is None)
r = assess_recovery({"recovery_requested": True, "recovery": None}, meta("no_fault"))
check("missing plan counts as invalid", r["valid"] is False and "no usable" in r["reason"], r)

log = run_service("pool_exhausted", 1).render()
good = json.dumps({"predicted_fault": "pool_exhausted", "explanation": "x", "confidence": 0.9, "recovery": {"action": "raise_pool_limit", "target": "connection-pool"}})
f = Fake(good)
d = diagnose(log, f, "pipeline", recovery=True)
check("recovery plan is parsed", d.recovery == {"action": "raise_pool_limit", "target": "connection-pool"} and d.recovery_requested)
check("system prompt was the recovery one", "failover_dependency" in f.system)
f2 = Fake(good)
d2 = diagnose(log, f2, "pipeline")
check("off by default", not d2.recovery_requested and "failover_dependency" not in f2.system)
same = Fake(good), Fake(good)
diagnose(log, same[0], "single_agent", recovery=True)
diagnose(log, same[1], "pipeline", recovery=True)
check("both conditions get the identical system prompt", same[0].system == same[1].system)
h = run_hypothesis(Fake('{"predicted_fault":"pool_exhausted","confidence":0.9,"recovery":"oops"}'), "p", recovery=True)
check("non-object recovery is ignored", h.recovery is None and not h.malformed)

m = {"red_herring_request_ids": [5], "injected_request_ids": [12]}
clean = {"claims_checked": 1, "claims_supported": 1, "cited_failure_ids": [12]}
L = lambda truth, d, g=1, rec=None: label_run(truth, d, m, g, rec)
check("clean correct run has no labels", L("pool_exhausted", {"predicted_fault": "pool_exhausted", "evidence": clean}) == [])
check("false alarm", L("no_fault", {"predicted_fault": "dependency_down", "evidence": clean}) == ["false_alarm"])
check("false alarm blaming a red herring", L("no_fault", {"predicted_fault": "dependency_down", "evidence": {**clean, "cited_failure_ids": [5]}}) == ["false_alarm", "blamed_red_herring"])
check("transient name counts as blame", "blamed_red_herring" in L("no_fault", {"predicted_fault": "dependency_down", "explanation": "a TimeoutError here", "evidence": clean}))
check("missed fault", L("slow_query", {"predicted_fault": "no_fault", "evidence": clean}) == ["missed_fault"])
check("spread vs burst", L("dependency_down", {"predicted_fault": "pool_exhausted", "evidence": clean}) == ["confused_spread_and_burst"])
check("latency misjudged", L("slow_query", {"predicted_fault": "dependency_down", "evidence": clean}) == ["misjudged_latency"])
check("invented evidence", "invented_evidence" in L("pool_exhausted", {"predicted_fault": "pool_exhausted", "evidence": {"claims_checked": 2, "claims_supported": 1}}))
check("ungrounded", L("pool_exhausted", {"predicted_fault": "pool_exhausted", "evidence": clean}, g=0) == ["right_but_ungrounded"])
check("invalid recovery", L("pool_exhausted", {"predicted_fault": "pool_exhausted", "evidence": clean}, rec={"valid": False}) == ["invalid_recovery"])
check("empty answer", L("no_fault", {"predicted_fault": None, "malformed": True, "parse_error": "empty response"}, g=0) == ["empty_answer"])
check("malformed output", L("no_fault", {"predicted_fault": "no_fault", "malformed": True, "parse_error": "no parseable"}, g=0) == ["malformed_output"])
check("failed call", L("no_fault", {"api_error": True}) == ["call_failed"])

tmp = pathlib.Path(tempfile.mkdtemp())
conn = db.connect(tmp / "x.db")
db.init_db(conn)
db.init_db(conn)
client = MockClient()
for fault in VALID_RECOVERY:
    run = run_service(fault, 1)
    for cond in ("single_agent", "pipeline"):
        result = diagnose(run.render(), client, cond, recovery=True).to_dict()
        db.record_run(conn, result, fault, fault, sweep_id="r", meta=run.meta())
row = analyze(conn, ["r"])["arms"][0]
check("mock recovery scored end to end", row["pipeline"]["recovery"]["rate"] == 1.0 and row["paired"]["recovery_valid"]["p"] == 1.0, row["pipeline"]["recovery"])
for cond in ("single_agent", "pipeline"):
    run = run_service("no_fault", 2)
    db.record_run(conn, diagnose(run.render(), client, cond).to_dict(), "plain", "no_fault", sweep_id="p", meta=run.meta())
plain = analyze(conn, ["p"])["arms"][0]
check("sweep without recovery has no rate", plain["pipeline"]["recovery"]["n"] == 0 and plain["pipeline"]["recovery"]["rate"] is None and plain["paired"]["recovery_valid"] == {"only_single": 0, "only_pipeline": 0, "p": 1.0})

print("\nFAILED: " + ", ".join(fails) if fails else "\nALL BATCH 2 CHECKS PASSED")
sys.exit(1 if fails else 0)
