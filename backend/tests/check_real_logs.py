"""Offline checks for real-log redaction, parsing and diagnosis. Run from backend/. No network."""

import json
import sys

sys.path.insert(0, ".")

from incident_response.providers.base import LLMResponse
from incident_response.providers.mock_client import MockClient
from incident_response.real_logs import diagnose_real, parse_line, preview, redact, sample_lines, summarize

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        fails.append(name)


# Made-up values only.
CASES = [
    ("email", "user=jane.doe@example.com failed", "jane.doe@example.com", "email"),
    ("ip", "connect from 10.2.3.4 refused", "10.2.3.4", "ip_address"),
    ("bearer", "Authorization: Bearer abcDEF123456789xyz", "abcDEF123456789xyz", "bearer_token"),
    ("groq key", "key gsk_aaaaaaaaaaaaaaaaaaaaaaaa used", "gsk_aaaaaaaaaaaaaaaaaaaaaaaa", "api_key"),
    ("password field", "password=hunter2 rejected", "hunter2", "secret_field"),
    ("json token", '{"api_key": "abc123secret"}', "abc123secret", "secret_field"),
    ("url credentials", "postgres://admin:s3cret@db.internal/app", "s3cret", "url_credentials"),
    ("valid card", "card 4111 1111 1111 1111 declined", "4111 1111 1111 1111", "card_number"),
]
for name, text, secret, kind in CASES:
    clean, counts = redact(text)
    check(f"redacts {name}", secret not in clean and counts.get(kind, 0) >= 1, clean)
clean, counts = redact("request 1234567890123 took 45ms")
check("ordinary long numbers survive", "1234567890123" in clean and "card_number" not in counts, clean)
clean, _ = redact("status=500 latency_ms=12.5 version=1.2.3")
check("metrics and versions survive", "latency_ms=12.5" in clean and "1.2.3" in clean, clean)

j = parse_line('{"level":"error","msg":"db down","duration_ms":120,"status":503}')
check("json line", j["shape"] == "json" and j["is_error"] and j["latency_ms"] == 120.0 and j["message"] == "db down", j)
k = parse_line('ts=2026-01-01 level=warn msg="slow call" latency_ms=900')
check("key=value line", k["shape"] == "key_value" and k["level"] == "WARN" and k["latency_ms"] == 900.0 and not k["is_error"], k)
t = parse_line("2026-01-01 10:00:00 ERROR ConnectionRefusedError: could not reach db took 2.5s")
check("plain text line", t["shape"] == "plain" and t["is_error"] and t["error_name"] == "ConnectionRefusedError" and t["latency_ms"] == 2500.0, t)
check("5xx status is an error", parse_line("level=info status=502 path=/x")["is_error"])
check("2xx status is not an error", not parse_line("level=info status=200 path=/x")["is_error"])
check("garbage does not crash", parse_line("{not json")["shape"] == "plain")

log = "\n".join(
    [f'{{"level":"info","msg":"ok {i}","latency_ms":{40 + i}}}' for i in range(50)]
    + [f'{{"level":"error","msg":"timeout calling svc-{i}","error_type":"UpstreamTimeout"}}' for i in range(10)]
)
s = summarize(log)
check("summary counts", s["total_lines"] == 60 and s["error_lines"] == 10 and s["latency_ms"]["count"] == 50, s["latency_ms"])
check("similar errors group", s["top_error_signatures"][0][1] == 10, s["top_error_signatures"])
check("errors located in the file", s["errors_by_tenth_of_file"][-1] > 0 and s["errors_by_tenth_of_file"][0] == 0)
check("sample is bounded and has errors", len(sample_lines(log)) <= 40 and any("error" in x for x in sample_lines(log)))
huge = "\n".join(f"level=error msg=boom{i} x=1" for i in range(20000))
check("sample stays bounded on a huge log", len(sample_lines(huge)) <= 40)
check("empty-ish log does not crash", summarize("")["total_lines"] == 0)


class Fake:
    provider = "groq"
    model = "fake"

    def __init__(self, text=None, error=None):
        self.text, self.error, self.user = text, error, None

    def complete(self, system, user, max_tokens=2048):
        self.user = user
        return LLMResponse(text=self.text or "", provider="groq", model="fake", error=self.error)


secret_log = "level=error msg=boom user=jane.doe@example.com\nlevel=info msg=fine x=1"
reply = {"hypotheses": [{"label": "dependency_down", "cause": "c", "confidence": 0.9, "evidence": ["msg=boom"]}], "next_checks": ["look"]}
fake = Fake(json.dumps(reply))
r = diagnose_real(secret_log, fake)
check("secrets never reach the model", "jane.doe" not in fake.user and "[REDACTED_EMAIL]" in fake.user)
check("confirmed high-confidence answer", r["needs_human"] is False and r["hypotheses"][0]["evidence"][0]["found_in_log"] is True, r["hypotheses"])
for label, change in [
    ("low confidence", {"confidence": 0.3}),
    ("unknown label", {"label": "unknown"}),
    ("invented quote", {"evidence": ["this line does not exist"]}),
]:
    check(f"{label} asks for a person", diagnose_real(secret_log, Fake(json.dumps({"hypotheses": [{**reply["hypotheses"][0], **change}]})))["needs_human"] is True)
check("no hypotheses asks for a person", diagnose_real(secret_log, Fake('{"hypotheses":[]}'))["needs_human"] is True)
check("broken reply is an error", "error" in diagnose_real(secret_log, Fake("nope")))
check("API failure is an error", "boom" in diagnose_real(secret_log, Fake(error="boom"))["error"])
check("mock works end to end", diagnose_real(secret_log, MockClient())["hypotheses"][0]["label"] != "")
p = preview(secret_log)
check("preview shows redaction and no secret", p["redactions"].get("email") == 1 and "jane.doe" not in json.dumps(p))

print("\nFAILED: " + ", ".join(fails) if fails else "\nALL REAL LOG CHECKS PASSED")
sys.exit(1 if fails else 0)
