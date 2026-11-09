"""Offline checks for error-count claims in the evidence scorer. Run from backend/. No network."""

import pathlib
import sys
import tempfile

sys.path.insert(0, ".")

from incident_response import db
from incident_response.analysis import analyze

from incident_response.agents.log_correlator import correlate
from incident_response.conditions import diagnose
from incident_response.evidence import score_evidence
from incident_response.providers.mock_client import MockClient
from incident_response.toy_service import run_service

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        fails.append(name)


# Noisy pool_exhausted run 1: 7 OperationalError on ids 12-18, plus TimeoutError on 9 and 11.
log = run_service("pool_exhausted", 1, noise=True).render()
summary = correlate(log)
check("fixture is as expected", summary.error_type_counts == {"TimeoutError": 2, "OperationalError": 7}
      and summary.error_count == 9, summary.error_type_counts)


def counts(text):
    report = score_evidence(text, log, summary)
    return report.count_claims_supported, report.count_claims_checked, report


ok, n, r = counts("9 OperationalError failures fall in a contiguous block of request ids.")
check("wrong typed count is flagged", (ok, n) == (0, 1) and "says 9 OperationalError but the log has 7" in r.count_unsupported,
      r.count_unsupported)
check("right typed count passes", counts("7 OperationalError failures fall in a contiguous block.")[:2] == (1, 1))
check("count after the type name", counts("OperationalError x7 and TimeoutError (2).")[:2] == (2, 2))
check("multiplication sign", counts("OperationalError ×7 on the burst")[:2] == (1, 1))
check("occurrences of", counts("Found 7 occurrences of OperationalError.")[:2] == (1, 1))
check("type absent from the log counts as zero", counts("3 PoolExhaustedError errors")[:2] == (0, 1))
check("total error count passes", counts("9 failed requests in all.")[:2] == (1, 1))
check("longest run passes as a count", counts("a run of 7 consecutive failures")[:2] == (1, 1))
ok, n, r = counts("12 errors overall")
check("invented total is flagged", (ok, n) == (0, 1) and r.count_unsupported, r.count_unsupported)
check("comparisons are not counts", counts("more than 3 errors, at least 2 failures")[:2] == (0, 0))
check("request ids are not counts",
      counts("Request 12 OperationalError began it; ids 12, 13, 14 OperationalError; requests 12-18 errors")[:2] == (0, 0))
check("latency is not a count", counts("read timed out after 2000ms")[:2] == (0, 0))
check("repeated claim counted once", counts("7 OperationalError failures. Again, 7 OperationalError.")[:2] == (1, 1))

# Count claims never move the existing score, so earlier sweeps stay comparable.
text = "9 OperationalError failures on requests 12-18, mean latency 48 ms."
with_counts = score_evidence(text, log, summary)
without = score_evidence("OperationalError failures on requests 12-18, mean latency 48 ms.", log, summary)
check("score and claims unchanged by counts",
      (with_counts.claims_checked, with_counts.claims_supported, with_counts.score)
      == (without.claims_checked, without.claims_supported, without.score),
      (with_counts.claims_checked, without.claims_checked))
d = with_counts.to_dict()
check("counts are reported", d["count_claims_checked"] == 1 and d["count_claims_supported"] == 0 and d["count_unsupported"])

# The mock used to count the TimeoutError transients as OperationalError.
mock = MockClient()
for condition in ("pipeline", "single_agent"):
    result = diagnose(log, mock, condition)
    ev = result.evidence or {}
    check(f"mock {condition} states the true count",
          "7 OperationalError" in result.explanation and ev.get("count_claims_supported") == ev.get("count_claims_checked") == 1,
          result.explanation)

# Every fault type: the mock's own explanations should never state a wrong count.
for fault in ("dependency_down", "pool_exhausted", "slow_query", "no_fault"):
    for noise in (False, True):
        for run in (1, 2, 3):
            text = run_service(fault, run, noise=noise).render()
            for condition in ("pipeline", "single_agent"):
                ev = diagnose(text, mock, condition).evidence or {}
                if ev.get("count_unsupported"):
                    check(f"mock count {fault} run{run} noise={noise} {condition}", False, ev["count_unsupported"])
check("mock counts agree with every generated log", not any(name.startswith("mock count") for name in fails))

# Analysis: count accuracy per condition, with runs scored before counts existed left out.
conn = db.connect(pathlib.Path(tempfile.mkdtemp()) / "runs.db")
db.init_db(conn)


def ev(checked, supported):
    return {"score": 1.0, "claims_checked": 1, "claims_supported": 1,
            "count_claims_checked": checked, "count_claims_supported": supported, "count_unsupported": []}


base = dict(provider="groq", model="m", predicted_fault="pool_exhausted", explanation="", remediation="")
for name, single_ev, pipe_ev in (
    ("a", ev(2, 1), ev(1, 1)),   # baseline stated one wrong count
    ("b", ev(1, 0), ev(0, 0)),   # pipeline stated no count
    ("c", {"score": 1.0, "claims_checked": 1, "claims_supported": 1}, ev(1, 1)),  # older baseline run
):
    db.record_run(conn, {**base, "condition": "single_agent", "evidence": single_ev}, name, "pool_exhausted", sweep_id="s")
    db.record_run(conn, {**base, "condition": "pipeline", "evidence": pipe_ev}, name, "pool_exhausted", sweep_id="s")
arm = analyze(conn, ["s"])["arms"][0]
single_counts, pipe_counts = arm["single_agent"]["counts"], arm["pipeline"]["counts"]
check("analysis counts baseline", (single_counts["k"], single_counts["n"], single_counts["runs_scored"],
                                   single_counts["runs_with_wrong_count"]) == (1, 3, 2, 2), single_counts)
check("analysis counts pipeline", (pipe_counts["k"], pipe_counts["n"], pipe_counts["rate"],
                                   pipe_counts["runs_with_wrong_count"]) == (2, 2, 1.0, 0), pipe_counts)
check("analysis count rate has an interval", single_counts["rate"] == 0.3333 and single_counts["ci95"] is not None)
check("accuracy unaffected by counts", arm["single_agent"]["accuracy"]["rate"] == 1.0 and arm["n_pairs"] == 3)
conn.close()

print("\nFAILED: " + ", ".join(fails) if fails else "\nALL EVIDENCE COUNT CHECKS PASSED")
sys.exit(1 if fails else 0)
