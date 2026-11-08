"""Offline key-store checks on throwaway files with made-up keys. No network."""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, ".")

from incident_response.key_store import IMPORTED_LABEL, KeyStore, KeyStoreError, key_id
from incident_response.providers import PROVIDERS, verify_key

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        fails.append(name)


tmp = pathlib.Path(tempfile.mkdtemp())
env = tmp / ".env"
store_path = tmp / "data" / "keys.json"
env.write_text("﻿# first comment\nALPHA_TEST_KEY=original-alpha-value-0001\n\n# keep me\nBETA_TEST_KEY=\nOTHER=1\n", encoding="utf-8")
os.environ["ALPHA_TEST_KEY"] = "original-alpha-value-0001"
os.environ.pop("BETA_TEST_KEY", None)
store = KeyStore(env, store_path, {"alpha": "ALPHA_TEST_KEY", "beta": "BETA_TEST_KEY"})
A1, A2, B1 = "original-alpha-value-0001", "second-alpha-value-00002", "first-beta-value-0000003"


def entries(provider):
    return store.listing()[provider]


listing_text = json.dumps(store.listing())
check("listing shows the .env key as in use", entries("alpha")[0]["active"] and entries("alpha")[0]["label"] == IMPORTED_LABEL)
check("listing never exposes the full key", A1 not in listing_text)
check("listing writes nothing to disk", not store_path.exists())

store.add("alpha", "work", A2, activate=True)
text = env.read_text(encoding="utf-8")
check(".env now holds the new key", f"ALPHA_TEST_KEY={A2}" in text and A1 not in text)
check("process environment follows", os.environ["ALPHA_TEST_KEY"] == A2)
check("comments and other lines survive", "# first comment" in text and "# keep me" in text and "OTHER=1" in text)
check("previous key was kept, not lost", any(e["value"] == A1 for e in json.loads(store_path.read_text())["alpha"]))
check("exactly one key is in use", sum(e["active"] for e in entries("alpha")) == 1)

store.activate("alpha", key_id(A1))
check("swap back works", os.environ["ALPHA_TEST_KEY"] == A1 and f"ALPHA_TEST_KEY={A1}" in env.read_text(encoding="utf-8"))

store.add("beta", "", B1, activate=False)
check("save only leaves provider without an active key", "BETA_TEST_KEY" not in os.environ)
check("blank name gets a default", entries("beta")[0]["label"].startswith("Key ending"), entries("beta")[0]["label"])

store.add("alpha", "renamed", A2, activate=False)
check("re-adding a key renames, no duplicate", len(entries("alpha")) == 2 and any(e["label"] == "renamed" for e in entries("alpha")))

store.remove("alpha", key_id(A2))
check("removing an unused key keeps the active one", os.environ["ALPHA_TEST_KEY"] == A1 and len(entries("alpha")) == 1)
store.remove("alpha", key_id(A1))
text = env.read_text(encoding="utf-8")
check("removing the active key blanks it", "ALPHA_TEST_KEY" not in os.environ and "ALPHA_TEST_KEY=\n" in text and entries("alpha") == [])
check("other provider untouched", len(entries("beta")) == 1)


def rejected(value):
    try:
        store.add("alpha", "", value)
        return False
    except KeyStoreError:
        return True


for name, value in [("short", "abc"), ("newline injection", "abcdefghijklmnopqrstuvwx\nEVIL=1"), ("space", "abcdefghij klmnopqrstuvwxyz"), ("hash", "abcdefghijklmnop#qrstuvwxyz"), ("quote", 'abcdefghijklmnop"qrstuvwxyz')]:
    check(f"rejects {name}", rejected(value))
check("rejected values wrote nothing", "EVIL" not in env.read_text(encoding="utf-8"))

for label, action in [("unknown id", lambda: store.activate("alpha", "nope")), ("keyless provider", lambda: store.add("mock", "", A1))]:
    try:
        action()
        check(f"refuses {label}", False)
    except KeyStoreError:
        check(f"refuses {label}", True)

store_path.write_text("{ damaged", encoding="utf-8")
try:
    store.add("alpha", "", A2)
    check("damaged store refused", False)
except KeyStoreError:
    check("damaged store refused", store_path.read_text(encoding="utf-8") == "{ damaged")
check("no temp files left behind", not list(tmp.rglob("*.tmp")))


class Boom(Exception):
    def __init__(self, status):
        super().__init__(f"Incorrect API key provided: {B1}")
        self.status_code = status


original = PROVIDERS["groq"]["verify"]
for status, want in [(401, "rejected"), (500, "HTTP 500")]:
    def fake(key, s=status):
        raise Boom(s)
    PROVIDERS["groq"]["verify"] = fake
    ok, message = verify_key("groq", B1)
    check(f"verify reports {status}", not ok and want in message and B1 not in message, message)
PROVIDERS["groq"]["verify"] = lambda key: None
check("verify accepts", verify_key("groq", B1) == (True, "Key accepted."))
PROVIDERS["groq"]["verify"] = original

from incident_response.evidence import score_evidence
from incident_response.agents.log_correlator import correlate
from incident_response.toy_service import run_service

_log = run_service("pool_exhausted", 1).render()
_sum = correlate(_log)
for text, want_checked, want_score, name in [
    ("A contiguous burst forms a tight block that then recovers at request 17.", 1, 1.0, "recovers at a healthy id"),
    ("The surrounding requests (e.g., 19, 20, 21) succeed normally.", 0, None, "healthy neighbours are not failure claims"),
    ("Latency is normal on request 3 overall.", 0, None, "mention with no claim is ignored"),
    ("Only two error lines appear (request_id=14 TimeoutError and request_id=16 ValidationError).", 4, 0.5, "id beside an error type is a claim"),
    ("The burst covers ids 12 and more; request 3 was slow.", 1, 1.0, "claim stops at the sentence end"),
]:
    r = score_evidence(text, _log, _sum).to_dict()
    check(name, r["claims_checked"] == want_checked and (want_score is None or abs(r["score"] - want_score) < 0.01), r)

print("\nFAILED: " + ", ".join(fails) if fails else "\nALL KEY CHECKS PASSED")
sys.exit(1 if fails else 0)
