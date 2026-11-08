"""After the full clean sweeps finish: use them in the report and rebuild it.

Run from the repository root:  python docs/milestone2/finish_clean.py
Add --dry to only print what it found.
"""

import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent.parent
TEMPLATE = pathlib.Path.home() / "Downloads" / "CSE598-capstone-milestone2-template.docx"
SINCE = "2026-11-08T22:50"
MODELS = {"qwen/qwen3.8-27b": "Qwen3.8 27B", "openai/gpt-oss-20b": "gpt-oss-20b", "openai/gpt-oss-120b": "gpt-oss-120b"}

db = sqlite3.connect(str(REPO / "backend/data/runs.db"))
data = json.loads((HERE / "report_results.json").read_text(encoding="utf-8"))
pairs, all_correct = {}, True
for model in MODELS:
    row = db.execute(
        "select sweep_id, count(*) n from runs where model=? and created_at>=? and sweep_id is not null "
        "and log_name not like '%noisy%' group by sweep_id order by n desc limit 1",
        (model, SINCE),
    ).fetchone()
    if not row:
        print("no new sweep for", model)
        continue
    sweep = row[0]
    data["sweeps"]["clean"][model] = sweep
    rows = db.execute(
        "select log_name, condition, correct from runs where sweep_id=? and model=? and api_error=0", (sweep, model)
    ).fetchall()
    by_log = {}
    for log, condition, correct in rows:
        by_log.setdefault(log, {})[condition] = correct
    done = [v for v in by_log.values() if len(v) == 2]
    pairs[model] = len(done)
    all_correct = all_correct and all(c == 1 for v in done for c in v.values())
    print(f"{model}: sweep {sweep}, {len(done)} pairs, all correct so far: {all(c == 1 for v in done for c in v.values())}")

if "--dry" in sys.argv:
    sys.exit()

counts = ", ".join(f"{n} for {MODELS[m]}" for m, n in pairs.items())
text = data["analysis"]
text[-1] = re.sub(r"D2 is a pilot subset with .*? omitted as too few\.", f"D2 has completed pairs of {counts}.", text[-1])
text[-1] = text[-1].replace("On D2 they flagged two runs", "On an earlier partial D2 run they flagged two runs").replace("slightly understate D2", "may understate D2")
if not all_correct:
    text[1] = "[[Update: not every D2 pair was correct on the full run. Check Tables 1 and 2 and rewrite this claim.]] " + text[1]
(HERE / "report_results.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
subprocess.run([sys.executable, str(HERE / "build_report.py"), str(TEMPLATE), "`b94b143` (tag `milestone-2`)"], check=True, env=env)
