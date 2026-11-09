"""RCAEval RE3 benchmark: from a case's logs, name the service that caused the failure.

This is a different task from the fault-type study, so it has its own loader, prompt
and scoring. Each case holds a large `logs.csv`, the time the fault was injected, and
a folder name that encodes the answer. The folder name is never shown to a model.

A case is far too large for a prompt, so two reductions are compared at one LLM call
each: an evenly spaced sample of raw lines, and a per-service summary comparing
behavior before and after the incident started.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass

from .providers.base import LLMResponse, extract_json
from .real_logs import _signature, redact

csv.field_size_limit(10_000_000)

ERROR_RE = re.compile(
    r"\b(?:error|errors|exception|fail(?:ed|ure|s)?|fatal|panic|traceback|refused|timed?\s?out|unavailable)\b|\bstatus[=: ]5\d\d\b|\s5\d\d\s",
    re.IGNORECASE,
)
MAX_SERVICES = 10
MAX_SIGNATURES = 3
MAX_SIGNATURE_CHARS = 150
RAW_SAMPLE_LINES = 40
RAW_LINE_CHARS = 200
MAX_TOKENS = 2048

PROMPT = """You are helping diagnose an incident in a microservice system. A fault was introduced \
into exactly one service at the moment labeled as the incident start. Decide which service is the root cause. \
Services that call the faulty service may also show errors, so look for where the problem starts.

Candidate services: {services}

Reply with one JSON object and nothing else:
{{"ranked_services": ["<most likely>", "<second>", "<third>"], "explanation": "<2-3 sentences citing the evidence>", "confidence": <0-1>}}
Use only names from the candidate list."""


@dataclass
class Case:
    case_id: str
    system: str
    root_cause: str
    fault: str
    services: list[str]
    summary: dict
    raw_sample: list[str]
    rules_ranking: list[str]
    total_lines: int


def list_cases(archive: str) -> list[str]:
    """Case directories in an archive, sorted so selection is repeatable."""
    with zipfile.ZipFile(archive) as bundle:
        return sorted({name.rsplit("/", 1)[0] for name in bundle.namelist() if name.endswith("/logs.csv")})


def pilot_selection(cases: list[str], count: int) -> list[str]:
    """Every n-th case, so the pilot spans the services and fault types in the archive."""
    if count >= len(cases):
        return cases
    step = len(cases) / count
    return [cases[int(index * step)] for index in range(count)]


def _build_summary(rows: list[tuple[str, str, bool]]) -> tuple[dict, list[str]]:
    """Per-service behavior before and after the incident. Rows are (service, message, after)."""
    stats: dict[str, dict] = {}
    for service, message, after in rows:
        entry = stats.setdefault(
            service,
            {"before": 0, "after": 0, "errors_before": 0, "errors_after": 0, "sig_before": Counter(), "sig_after": Counter()},
        )
        entry["after" if after else "before"] += 1
        if ERROR_RE.search(message):
            entry["errors_after" if after else "errors_before"] += 1
            entry["sig_after" if after else "sig_before"][_signature(message)] += 1

    def change(entry: dict) -> tuple[float, float]:
        before_rate = entry["errors_before"] / max(1, entry["before"])
        after_rate = entry["errors_after"] / max(1, entry["after"])
        volume = abs(entry["after"] - entry["before"]) / max(1, entry["before"])
        return (after_rate - before_rate, volume)

    ordered = sorted(stats, key=lambda name: (change(stats[name]), name), reverse=True)
    services = []
    for name in ordered[:MAX_SERVICES]:
        entry = stats[name]
        new = [(sig, n) for sig, n in entry["sig_after"].most_common() if sig not in entry["sig_before"]]
        services.append(
            {
                "service": name,
                "lines_before": entry["before"],
                "lines_after": entry["after"],
                "error_lines_before": entry["errors_before"],
                "error_lines_after": entry["errors_after"],
                "new_error_messages_after_start": [
                    {"message": sig[:MAX_SIGNATURE_CHARS], "count": n} for sig, n in new[:MAX_SIGNATURES]
                ],
            }
        )
    return {"services_with_largest_change_first": services}, ordered


def _parse_folder(folder: str) -> tuple[str, str]:
    """Folders are <service>_f<n>, and some archives add a further suffix."""
    match = re.match(r"(.+?)_(f\d+)(?:_.*)?$", folder)
    return (match.group(1), match.group(2)) if match else (folder, "")


def load_case(archive: str, root: str) -> Case:
    with zipfile.ZipFile(archive) as bundle:
        start_ns = int(bundle.read(f"{root}/inject_time.txt").decode().strip()) * 1_000_000_000
        text = bundle.read(f"{root}/logs.csv").decode("utf-8", "replace")

    rows: list[tuple[str, str, bool]] = []
    for row in csv.DictReader(io.StringIO(text)):
        service, message = row.get("container_name") or "", row.get("message") or ""
        try:
            stamp = int(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if service:
            rows.append((service, message, stamp >= start_ns))

    parts = root.split("/")
    folder = parts[-2]
    service, fault = _parse_folder(folder)
    summary, ranking = _build_summary(rows)

    after = [row for row in rows if row[2]]
    step = max(1, len(after) // RAW_SAMPLE_LINES)
    sample = [f"[{name}] {message[:RAW_LINE_CHARS]}" for name, message, _ in after[::step][:RAW_SAMPLE_LINES]]
    return Case(
        case_id=f"{parts[0]}/{folder}/{parts[-1]}",
        system=parts[0],
        root_cause=service,
        fault=fault,
        services=sorted({row[0] for row in rows}),
        summary=summary,
        raw_sample=sample,
        rules_ranking=ranking,
        total_lines=len(rows),
    )


def payload_for(case: Case, condition: str) -> str:
    if condition == "raw_sample":
        body = "Evenly spaced log lines from after the incident started:\n" + "\n".join(case.raw_sample)
    elif condition == "summary":
        body = "Log summary comparing each service before and after the incident started:\n" + json.dumps(case.summary, indent=1)
    else:
        raise ValueError(condition)
    return redact(body)[0]


def score(ranked: list[str], root_cause: str) -> tuple[int, int]:
    """Top-1 and Top-3 hits."""
    return int(ranked[:1] == [root_cause]), int(root_cause in ranked[:3])


def parse_ranking(response: LLMResponse, services: list[str]) -> dict:
    """Read a model's ranking, keeping only real candidates."""
    if not response.ok:
        return {"ranked": [], "api_error": True, "malformed": False, "explanation": "", "confidence": None, "error": response.error}
    parsed, error = extract_json(response.text)
    if parsed is None or not isinstance(parsed.get("ranked_services"), list):
        return {"ranked": [], "api_error": False, "malformed": True, "explanation": "", "confidence": None, "error": error or "no ranked_services"}
    lookup = {name.lower(): name for name in services}
    ranked: list[str] = []
    for item in parsed["ranked_services"]:
        name = lookup.get(str(item).strip().lower())
        if name and name not in ranked:
            ranked.append(name)
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence"))))
    except (TypeError, ValueError):
        confidence = None
    return {"ranked": ranked, "api_error": False, "malformed": False, "explanation": str(parsed.get("explanation", ""))[:600], "confidence": confidence, "error": None}


def system_prompt(case: Case) -> str:
    return PROMPT.format(services=", ".join(case.services))
