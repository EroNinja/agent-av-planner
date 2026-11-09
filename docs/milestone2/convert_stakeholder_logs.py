"""Convert the stakeholder's published logs to this system's line format.

Only the field order, the level field and the case of the status value change.
Usage: python convert_stakeholder_logs.py <her logs dir> <output dir>
"""

import json
import pathlib
import re
import sys

SOURCE = pathlib.Path(sys.argv[1])
TARGET = pathlib.Path(sys.argv[2])
TARGET.mkdir(parents=True, exist_ok=True)

LINE = re.compile(
    r'^(?P<ts>\S+) request_id=(?P<id>\d+)(?: latency_ms=(?P<latency>[\d.]+))? status=(?P<status>OK|ERROR)'
    r'(?: latency_ms=(?P<latency2>[\d.]+))?(?: error_type=(?P<etype>\S+))?(?: message="(?P<message>[^"]*)")?$'
)

total = 0
for log in sorted(SOURCE.glob("*.log")):
    meta = json.loads(log.with_suffix(".meta.json").read_text(encoding="utf-8"))
    output, failing, seen = [], [], []
    for raw in log.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        match = LINE.match(raw.strip())
        if match is None:
            raise SystemExit(f"unrecognized line in {log.name}: {raw[:120]}")
        request_id = int(match["id"])
        is_error = match["status"] == "ERROR"
        seen.append(request_id)
        if is_error:
            failing.append(request_id)
        latency = match["latency"] or match["latency2"]
        parts = [match["ts"], f"level={'ERROR' if is_error else 'INFO'}", f"request_id={request_id}", f"status={'error' if is_error else 'ok'}"]
        if latency:
            parts.append(f"latency_ms={latency}")
        if match["etype"]:
            parts.append(f"error_type={match['etype']}")
        if match["message"] is not None:
            parts.append(f'message="{match["message"]}"')
        output.append(" ".join(parts))
        total += 1
    (TARGET / log.name).write_text("\n".join(output) + "\n", encoding="utf-8")
    converted = {
        "fault_type": meta["fault_type"],
        "run_id": meta["run_id"],
        "request_count": len(seen),
        "noise": False,
        # Her logs carry no red herrings, so every failing request is an injected one.
        "injected_request_ids": failing,
        "red_herring_request_ids": [],
    }
    (TARGET / log.with_suffix(".meta.json").name).write_text(json.dumps(converted, indent=2) + "\n", encoding="utf-8")
print(f"converted {len(list(TARGET.glob('*.log')))} logs, {total} lines")
