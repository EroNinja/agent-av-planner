"""Drive a freshly cloned backend through its HTTP API, offline, using the mock.

E2E_BASE_URL points it at another backend, such as the Docker container.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8123").rstrip("/")
fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        fails.append(name)


def call(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


status, text = call("/api/health")
check("health", status == 200)
providers = json.loads(call("/api/providers")[1])["providers"]
state = {p["provider"]: p["available"] for p in providers}
check("fresh clone has no keys", state == {"anthropic": False, "groq": False, "mock": True}, state)
check("no keys are listed", all(not p["keys"] for p in json.loads(call("/api/keys")[1])["providers"]))

status, text = call("/api/logs/generate", "POST", {"suite": True, "runs_per_fault": 2, "noise": True})
logs = json.loads(text)["generated"]
check("generate logs", status == 200 and len(logs) == 8, len(logs))
name = next(log["name"] for log in logs if log["true_fault"] == "pool_exhausted")

for condition, calls in (("single_agent", 1), ("pipeline", 1), ("pipeline_no_spacing", 1), ("tool_agent", 3)):
    status, text = call("/api/diagnose/stream", "POST", {"log_name": name, "condition": condition, "provider": "mock", "recovery": True})
    ev = events(text)
    result = next((e for e in ev if e["type"] == "result"), None)
    check(f"diagnose {condition}", status == 200 and result and result["result"]["predicted_fault"] == "pool_exhausted" and result["result"]["llm_call_count"] == calls)
    check(f"recovery replay {condition}", bool(result and result["recovery_result"] and result["recovery_result"]["valid"]))

status, text = call("/api/diagnose/stream", "POST", {"log_name": name, "condition": "pipeline", "provider": "groq"})
check("missing key is a clear error", status == 400 and "GROQ_API_KEY" in text, text[:90])
status, text = call("/api/sweep", "POST", {"arms": ["mock:", "groq:x"]})
check("mixed sweep rejected", status == 400)
status, text = call("/api/sweep", "POST", {"arms": ["mock:"], "conditions": ["single_agent", "pipeline", "pipeline_no_spacing", "tool_agent"], "recovery": True})
sweep = json.loads(text)
check("sweep starts", status == 200 and sweep["total"] == 32, sweep.get("total"))
ev = events(call(f"/api/sweep/{sweep['sweep_id']}/stream")[1])
check("sweep completes", ev[-1]["type"] == "sweep_done" and ev[-1]["completed"] == 32, ev[-1])

stats = json.loads(call(f"/api/stats?sweep_id={sweep['sweep_id']}")[1])
check("stats", len(stats["by_arm"]) == 4, len(stats["by_arm"]))
analysis = json.loads(call(f"/api/analysis?sweep_ids={sweep['sweep_id']}")[1])["arms"][0]
check("analysis covers extra conditions", set(analysis["others"]) == {"pipeline_no_spacing", "tool_agent"} and analysis["pipeline"]["recovery"]["n"] == 8)
runs = json.loads(call(f"/api/runs?sweep_id={sweep['sweep_id']}")[1])["runs"]
check("runs recorded", len(runs) == 32 and "failure_labels" in runs[0])
check("run detail", json.loads(call(f"/api/runs/{runs[0]['id']}")[1])["id"] == runs[0]["id"])

fake_key = "gsk_" + "b" * 30
status, text = call("/api/keys", "POST", {"provider": "groq", "label": "e2e", "value": fake_key, "activate": True})
listed = json.loads(text)["providers"]
check("key can be saved", status == 200 and any(k["label"] == "e2e" and k["active"] for p in listed for k in p["keys"]))
check("full key is never returned", fake_key not in text)
key_id = next(k["id"] for p in listed for k in p["keys"] if k["label"] == "e2e")
status, text = call(f"/api/keys/groq/{key_id}", "DELETE")
check("key can be removed", status == 200 and fake_key not in text)

log = 'level=error msg="db failed for jane@example.com" latency_ms=30\nlevel=info msg=ok latency_ms=20'
status, text = call("/api/real-log/preview", "POST", {"text": log})
check("real-log redaction", status == 200 and "jane@example.com" not in text and "REDACTED_EMAIL" in text)
status, text = call("/api/real-log/diagnose", "POST", {"text": log, "provider": "mock"})
check("real-log diagnosis", status == 200 and "hypotheses" in json.loads(text))
check("empty real log rejected", call("/api/real-log/preview", "POST", {"text": "  "})[0] == 400)
check("oversize real log rejected", call("/api/real-log/preview", "POST", {"text": "x" * 5_000_001})[0] == 413)

req = urllib.request.Request(BASE + "/api/keys")
req.add_header("Host", "evil.example")
try:
    urllib.request.urlopen(req, timeout=10)
    check("foreign Host rejected", False)
except urllib.error.HTTPError as exc:
    check("foreign Host rejected", exc.code == 400)

print("\nFAILED: " + ", ".join(fails) if fails else "\nALL END-TO-END CHECKS PASSED")
sys.exit(1 if fails else 0)
