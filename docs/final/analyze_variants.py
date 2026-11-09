"""Compare the fixed pipeline, no-spacing parser and earlier baseline on D1. Read-only."""
import json
import sqlite3
import sys

sys.path.insert(0, "backend")
sys.stdout.reconfigure(encoding="utf-8")
from incident_response.analysis import exact_mcnemar

c = sqlite3.connect("backend/data/runs.db")
c.row_factory = sqlite3.Row
OLD = "8db7c2f7db03"


def rows(sweep, model, cond):
    return {r["log_name"]: r for r in c.execute("select * from runs where sweep_id=? and model=? and condition=? and api_error=0", (sweep, model, cond))}


for model in ("qwen/qwen3.8-27b", "openai/gpt-oss-120b"):
    sweep = c.execute("select sweep_id from runs where model=? and condition='pipeline_no_spacing' order by id desc limit 1", (model,)).fetchone()[0]
    failed = c.execute("select count(*), max(substr(parse_error,1,150)) from runs where sweep_id=? and api_error=1", (sweep,)).fetchone()
    new_p, nosp = rows(sweep, model, "pipeline"), rows(sweep, model, "pipeline_no_spacing")
    base, old_p = rows(OLD, model, "single_agent"), rows(OLD, model, "pipeline")
    common = new_p.keys() & nosp.keys() & base.keys() & old_p.keys()
    print(f"\n{model} sweep {sweep} failed={tuple(failed)} common logs={len(common)}")
    for label, data in (("A raw (earlier)", base), ("old pipeline", old_p), ("no spacing", nosp), ("fixed pipeline", new_p)):
        sub = [data[k] for k in common]
        print(f"  {label:<16} acc {sum(r['correct'] == 1 for r in sub)}/{len(sub)}  malformed {sum(r['malformed'] for r in sub)}  in {sum(r['input_tokens'] for r in sub)/len(sub):.0f} out {sum(r['output_tokens'] for r in sub)/len(sub):.0f}")
    for a_name, a, b_name, b in (("fixed", new_p, "no spacing", nosp), ("fixed", new_p, "A raw", base), ("no spacing", nosp, "A raw", base), ("fixed", new_p, "old pipeline", old_p)):
        only_a = sum(a[k]["correct"] == 1 and b[k]["correct"] != 1 for k in common)
        only_b = sum(a[k]["correct"] != 1 and b[k]["correct"] == 1 for k in common)
        print(f"  {a_name} vs {b_name}: only {a_name}={only_a} only {b_name}={only_b} p={exact_mcnemar(only_a, only_b):.3f}")
    for label, data in (("no spacing", nosp), ("fixed", new_p)):
        for k in sorted(common):
            r = data[k]
            if r["correct"] != 1:
                print(f"  MISS {label}: {k} predicted {r['predicted_fault']} labels {r['failure_labels']} | {(r['explanation'] or '')[:170]}")
