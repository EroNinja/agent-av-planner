"""Offline checks for scoring, sweeps and analysis. Run from backend/. No network."""
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, ".")

from incident_response import db, evaluation
from incident_response.agents.hypothesis_agent import raw_log_payload, summary_payload, system_prompt
from incident_response.agents.log_correlator import _percentile, correlate
from incident_response.conditions import diagnose
from incident_response.evaluation import Arm, balanced_order, run_sweep_stream, validate_arms
from incident_response.evidence import score_evidence
from incident_response.log_generator import LogRecord, generate_suite
from incident_response.providers.base import LLMResponse
from incident_response.toy_service import run_service

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        fails.append(name)


class Fake:
    provider = "groq"
    model = "fake"

    def __init__(self, text="", error=None):
        self.text, self.error = text, error

    def complete(self, system, user, max_tokens=4096):
        return LLMResponse(text=self.text, provider="groq", model="fake", error=self.error)


log = run_service("pool_exhausted", 1).render()
summary = correlate(log)

# 1. prompt parity
sp, raw, summ = system_prompt(), raw_log_payload(log), summary_payload(summary.to_dict())
check("baseline in shared system prompt", "45ms" in sp)
check("baseline not in either payload", "45ms" not in raw and "45ms" not in summ)

# 2. scoring
broken = '{\n "predicted_fault": "pool_exhausted",\n "explanation": "x",\n "confidence": 0. nine,\n "ranked_alternatives": []\n}'
d = diagnose(log, Fake(broken), "single_agent")
check("broken JSON keeps the label", d.predicted_fault == "pool_exhausted" and d.malformed and not d.api_error)
check("broken JSON is not evidence-scored", d.evidence["claims_checked"] == 0)
d2 = diagnose(log, Fake(error="APIError: boom"), "single_agent")
check("API failure is api_error, not malformed", d2.api_error and not d2.malformed and d2.predicted_fault is None)
d3 = diagnose(log, Fake('{"predicted_fault":"made_up","confidence":0.5}'), "pipeline")
check("off-taxonomy label stays unanswered", d3.malformed and d3.predicted_fault is None)

# 3. noise giveaway
for fault in ("pool_exhausted", "dependency_down"):
    noisy = run_service(fault, 1, noise=True).render()
    check(f"noisy {fault} has no identifying name",
          "PoolExhaustedError" not in noisy and "ConnectionRefusedError" not in noisy and "OperationalError" in noisy)
check("clean logs keep the proposal's error names", "PoolExhaustedError" in log)
check("noise is still deterministic", run_service("pool_exhausted", 3, noise=True).render() == run_service("pool_exhausted", 3, noise=True).render())

# 4/5. evidence (true failures in this log are 12-16)
def ev(text):
    return score_evidence(text, log, summary).to_dict()

r = ev("ConnectionRefused at request_id=2,4,8,10.")
check("request_id=a,b,c is checked", r["claims_checked"] == 4 and r["claims_supported"] == 0, r["score"])
r = ev("Errors at request_ids 12, 13 and more.")
check("request_ids a, b is checked", r["claims_checked"] == 2 and r["score"] == 1.0, r["score"])
r = ev("Failures in requests 12-16 only.")
check("range counts endpoints", r["claims_checked"] == 2 and r["score"] == 1.0, r["score"])
r = ev("REQUESTS 12–16 failed.")
check("uppercase and en dash range", r["claims_checked"] == 2 and r["score"] == 1.0, r["score"])
r = ev("Failures span ids 12 through 16, spread out.")
check("span does not claim the interior", r["claims_checked"] == 2 and r["score"] == 1.0, r["score"])
r = ev("Requests 8-11 failed.")
check("wrong range is flagged", r["score"] == 0.0, r["score"])
r = ev("The surviving calls (e.g., 33.26 ms for request 12, 57.44 ms for request 15) look fine.")
check("decimal is not an id; located latency is neutral", r["claims_checked"] == 2 and r["cited_failure_ids"] == [], r)
r = ev("Latencies are normal (e.g., 24.14 ms at id 0, 43.58 ms at id 2).")
check("id list stops at a decimal", r["cited_failure_ids"] == [] and "request id 43" not in str(r), r)
r = ev("Successful requests (e.g., request_id 3 latency 40.5ms) stay normal.")
check("success with e.g. is not a failure claim", r["cited_failure_ids"] == [] and r["score"] == 1.0, r)
r = ev("Request 3 succeeded normally.")
check("true success claim is supported", r["score"] == 1.0, r)
r = ev("Request 13 succeeded normally.")
check("false success claim is flagged", r["score"] == 0.0, r)
r = ev("Requests 0-9 succeeded before the burst at requests 12-16.")
check("mixed polarity sentence", r["claims_checked"] == 4 and r["score"] == 1.0, r["score"])
r = ev("Errors at ids 12, 13 are interleaved with successful requests.")
check("nearby 'successful' does not flip polarity", r["score"] == 1.0, r)
r = ev("Latency stayed near 1 ms.")
check("impossibly low latency is flagged", r["score"] == 0.0, r)
r = ev("Latency stayed under 100 ms throughout.")
check("threshold is not a measurement", r["claims_checked"] == 0, r)
r = ev("30 requests were served.")
check("a count is not an id", r["claims_checked"] == 0, r)
r = ev("Request 99 failed.")
check("out-of-run id is flagged", r["score"] == 0.0, r)

# 6. p90
check("p90 of 1..30 is 27", _percentile([float(i) for i in range(1, 31)], 90) == 27.0)

# DB, on throwaway copies
tmp = pathlib.Path(tempfile.mkdtemp())
old = tmp / "old.db"
_legacy = sqlite3.connect(old)
_legacy.execute("create table runs (id integer primary key, created_at text, sweep_id text, provider text, model text, condition text)")
_legacy.executemany("insert into runs (created_at) values (?)", [("a",), ("b",), ("c",)])
_legacy.commit()
_legacy.close()
conn = db.connect(old)
before = conn.execute("select count(*) from runs").fetchone()[0]
db.init_db(conn)
db.init_db(conn)
check("migration adds api_error, keeps rows",
      "api_error" in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
      and conn.execute("select count(*) from runs").fetchone()[0] == before, before)
conn.close()

conn = db.connect(tmp / "new.db")
db.init_db(conn)
base = dict(condition="single_agent", provider="groq", model="m", explanation="", remediation="", evidence={})
rows = [
    ("no_fault", dict(predicted_fault="no_fault")),
    ("no_fault", dict(predicted_fault="slow_query")),
    ("no_fault", dict(predicted_fault=None, malformed=True)),
    ("no_fault", dict(predicted_fault="no_fault", malformed=True)),
    ("no_fault", dict(predicted_fault=None, api_error=True)),
]
for truth, extra in rows:
    db.record_run(conn, {**base, **extra}, "l", truth, sweep_id="s1")
db.record_run(conn, {**base, "provider": "mock", "model": "x", "predicted_fault": "no_fault"}, "l", "no_fault", sweep_id="s2")
row = [r for r in db.aggregate(conn, sweep_id="s1")][0]
check("failed call excluded from n", row["n"] == 4 and row["n_failed"] == 1, (row["n"], row["n_failed"]))
check("accuracy counts the recovered label", abs(row["accuracy"] - 0.5) < 1e-9, row["accuracy"])
check("strict excludes broken JSON", abs(row["strict_accuracy"] - 0.25) < 1e-9, row["strict_accuracy"])
fault = db.aggregate_by_fault(conn, sweep_id="s1")[0]
check("false positive counts only real fault claims", abs(fault["fault_claim_rate"] - 0.25) < 1e-9, fault["fault_claim_rate"])
pooled = db.aggregate(conn, exclude_providers=["mock"])
check("pooled view excludes mock", {r["provider"] for r in pooled} == {"groq"})
check("mock-only sweep still viewable", db.aggregate(conn, sweep_id="s2")[0]["provider"] == "mock")
check("latest sweep resolves", db.latest_sweep(conn) == "s2")

# sweep safety
try:
    validate_arms([Arm("mock", "x"), Arm("groq", "y")])
    check("mixed arms rejected", False)
except ValueError:
    check("mixed arms rejected", True)
validate_arms([Arm("groq", "a"), Arm("anthropic", "b")])

records = generate_suite(tmp / "logs", ["dependency_down", "no_fault", "pool_exhausted", "slow_query"], 3, noise=True)
order = [r.name.split("_run")[0] for r in balanced_order(records)][:4]
check("round one holds every fault once", sorted(order) == ["dependency_down", "no_fault", "pool_exhausted", "slow_query"], order)

calls = {"n": 0}


class Counting(Fake):
    def complete(self, system, user, max_tokens=4096):
        calls["n"] += 1
        return super().complete(system, user, max_tokens)


evaluation.get_client = lambda p, m: Counting(error="RateLimitError: 429 rate limit reached on tokens per day (TPD)")
conn2 = db.connect(tmp / "s.db")
db.init_db(conn2)
events = list(run_sweep_stream(records, [Arm("groq", "m")], conn2))
check("daily cap stops the arm at once", calls["n"] == 1 and any(e["type"] == "arm_failed" for e in events), calls["n"])
check("progress still reaches total", events[-1]["completed"] == events[-1]["total"], events[-1]["completed"])

calls["n"] = 0
evaluation.get_client = lambda p, m: Counting(error="APIConnectionError: connection reset")
events = list(run_sweep_stream(records, [Arm("groq", "m")], conn2))
check("non-rate-limit failures stop after 3", calls["n"] == 3, calls["n"])

# ---- Batch 1
from incident_response.analysis import analyze, brier_score, exact_mcnemar, expected_calibration_error, wilson_interval
from incident_response.grounding import assess
from incident_response.log_generator import scenario_name

check("McNemar 0 vs 3 is 0.25", exact_mcnemar(0, 3) == 0.25)
check("McNemar with no discordant pairs is 1", exact_mcnemar(0, 0) == 1.0)
check("McNemar 0 vs 6 is 0.03125", exact_mcnemar(0, 6) == 0.03125)
lo, hi = wilson_interval(37, 40)
check("Wilson interval matches reference", abs(lo - 0.8014) < 0.002 and abs(hi - 0.9742) < 0.002, (lo, hi))
check("Brier of perfect confidence is 0", brier_score([(1.0, 1), (0.0, 0)]) == 0.0)
check("ECE of 0.9 confidence at 50% is 0.4", abs(expected_calibration_error([(0.9, 1), (0.9, 0)]) - 0.4) < 1e-9)

meta = {"injected_request_ids": [12, 13], "request_count": 30}
good = {"predicted_fault": "pool_exhausted", "evidence": {"claims_checked": 2, "claims_supported": 2, "cited_failure_ids": [12]}}
check("correct, clean, cites cause", assess("pool_exhausted", good, meta) == (1, 1))
luck = {"predicted_fault": "pool_exhausted", "evidence": {"claims_checked": 1, "claims_supported": 1, "cited_failure_ids": [5]}}
check("correct but cites only a red herring", assess("pool_exhausted", luck, meta) == (1, 0))
fab = {"predicted_fault": "pool_exhausted", "evidence": {"claims_checked": 2, "claims_supported": 1, "cited_failure_ids": [12]}}
check("invented evidence fails the proposal", assess("pool_exhausted", fab, meta) == (0, 0))
check("wrong label fails", assess("slow_query", good, meta) == (0, 0))
check("broken JSON fails both", assess("pool_exhausted", {**good, "malformed": True}, meta) == (0, 0))
check("failed call has no verdict", assess("pool_exhausted", {"api_error": True}, meta) == (None, None))
slow = {"predicted_fault": "slow_query", "evidence": {"claims_checked": 1, "claims_supported": 1, "latency_supported": 1}}
check("slow_query grounded by a latency", assess("slow_query", slow, meta) == (1, 1))
nolat = {"predicted_fault": "slow_query", "evidence": {"claims_checked": 0, "claims_supported": 0}}
check("slow_query without a figure is not grounded", assess("slow_query", nolat, meta) == (1, 0))

r = ev("Errors hit error_request_ids 12,13,14 only.")
check("error_request_ids is recognized", r["cited_failure_ids"] == [12, 13, 14], r)
r = ev("Requests 13-14 are errors, then request 17 resumes normal ok status.")
check("resumes is a success claim", r["score"] == 1.0 and 17 not in r["cited_failure_ids"], r)
r = ev("The 11 errors are spread (max_contiguous_run = 2, mean_gap = 2.4).")
check("spacing evidence is detected", r["spacing_cited"] is True, r)
check("spacing grounds a spread fault", assess("dependency_down", {"predicted_fault": "dependency_down", "evidence": r}, meta) == (1, 1))
r = ev("Latency is high, e.g. request_id=0 latency_ms=40.48.")
check("latency_ms=value counts as a measured latency", r["latency_supported"] == 1, r)
r = ev("Latency is high, e.g. latency_ms=9000.5.")
check("impossible latency_ms value does not ground", r["latency_supported"] == 0, r)

check("default length keeps old name", scenario_name("no_fault", 1, True) == "no_fault_run1_noisy")
check("other lengths get a suffix", scenario_name("no_fault", 1, True, 75) == "no_fault_run1_noisy_r75")

conn3 = db.connect(tmp / "a.db")
db.init_db(conn3)
for log, single, pipe in [("a", 1, 1), ("b", 0, 1), ("c", 0, 1)]:
    for cond, value in (("single_agent", single), ("pipeline", pipe)):
        d = {"condition": cond, "provider": "groq", "model": "m", "predicted_fault": "no_fault" if value else "slow_query", "confidence": 0.9, "explanation": "", "remediation": "", "evidence": {}}
        db.record_run(conn3, d, log, "no_fault", sweep_id="z", meta={"request_count": 30, "injected_request_ids": []})
arm = analyze(conn3, ["z"])["arms"][0]
check("analysis pairs logs across conditions", arm["n_pairs"] == 3 and arm["paired"]["correct"] == {"only_single": 0, "only_pipeline": 2, "p": 0.5}, arm["paired"]["correct"])
check("analysis reports the length", arm["request_count"] == 30)
check("empty sweep list is safe", analyze(conn3, []) == {"sweep_ids": [], "arms": []})

print("\nFAILED: " + ", ".join(fails) if fails else "\nALL CHECKS PASSED")
sys.exit(1 if fails else 0)
