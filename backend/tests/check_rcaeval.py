"""Offline checks for the RCAEval adapter using a small synthetic archive. Run from backend/."""

import pathlib
import sys
import tempfile
import zipfile

sys.path.insert(0, ".")

from incident_response.providers.base import LLMResponse
from incident_response.rcaeval import (
    _parse_folder,
    list_cases,
    load_case,
    parse_ranking,
    payload_for,
    pilot_selection,
    score,
    system_prompt,
)

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        fails.append(name)


check("plain folder", _parse_folder("carts_f1") == ("carts", "f1"))
check("hyphenated service", _parse_folder("ts-auth-service_f2") == ("ts-auth-service", "f2"))
check("extra suffix is not part of the service", _parse_folder("ts-route-service_f3_1") == ("ts-route-service", "f3"))

START = 1_000
rows = ["time,timestamp,container_name,message,pod_name,node_name"]
for i in range(200):
    rows.append(f"t,{(START - 100 + i) * 1_000_000_000},web,GET /home 200,p,n")
for i in range(50):
    rows.append(f"t,{(START - 100 + i) * 1_000_000_000},db,query ok,p,n")
for i in range(40):
    rows.append(f'''t,{(START + i) * 1_000_000_000},db,"ERROR connection failed for jane@example.com",p,n''')
rows.append(f"t,{START * 1_000_000_000},,no service name,p,n")
rows.append("t,bad-timestamp,web,skipped,p,n")

path = pathlib.Path(tempfile.mkdtemp()) / "RE3-XX.zip"
with zipfile.ZipFile(path, "w") as bundle:
    for run in (1, 2):
        bundle.writestr(f"RE3-XX/db_f1/{run}/logs.csv", "\n".join(rows))
        bundle.writestr(f"RE3-XX/db_f1/{run}/inject_time.txt", str(START))

cases = list_cases(str(path))
check("cases are listed", cases == ["RE3-XX/db_f1/1", "RE3-XX/db_f1/2"], cases)
check("pilot takes a spread", pilot_selection(list(range(30)), 10) == list(range(0, 30, 3)))
check("pilot handles small archives", pilot_selection([1, 2], 10) == [1, 2])

case = load_case(str(path), cases[0])
check("truth comes from the folder", case.root_cause == "db" and case.fault == "f1")
check("services exclude blank names", case.services == ["db", "web"], case.services)
check("malformed rows are skipped", case.total_lines == 290, case.total_lines)
check("rules rank the erroring service first", case.rules_ranking[0] == "db", case.rules_ranking)
db = case.summary["services_with_largest_change_first"][0]
check("before and after are separated", db["lines_before"] == 50 and db["lines_after"] == 40 and db["error_lines_after"] == 40, db)
check("new errors are reported", db["new_error_messages_after_start"][0]["count"] == 40)
for condition in ("raw_sample", "summary"):
    text = payload_for(case, condition)
    check(f"{condition} is redacted", "jane@example.com" not in text)
    check(f"{condition} hides the answer folder", "db_f1" not in text and "RE3-XX" not in text)
check("prompt lists candidates only", "db, web" in system_prompt(case) and "db_f1" not in system_prompt(case))


def response(text, error=None):
    return LLMResponse(text=text, provider="x", model="y", error=error)


parsed = parse_ranking(response('{"ranked_services":["DB","nope","web","db"],"confidence":0.8}'), case.services)
check("ranking keeps real unique candidates", parsed["ranked"] == ["db", "web"], parsed["ranked"])
check("scoring", score(["db", "web"], "db") == (1, 1) and score(["web", "db"], "db") == (0, 1) and score(["web"], "db") == (0, 0))
check("broken reply is malformed", parse_ranking(response("nope"), case.services)["malformed"] is True)
check("API failure is separate", parse_ranking(response("", "429"), case.services)["api_error"] is True)

print("\nFAILED: " + ", ".join(fails) if fails else "\nALL RCAEVAL CHECKS PASSED")
sys.exit(1 if fails else 0)
