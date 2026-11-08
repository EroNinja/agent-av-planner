"""Write toy-service runs to disk as `<fault>_run<N>.log` + `<fault>_run<N>.meta.json`.

The `.log` is the only thing any model is ever shown. The `.meta.json` holds the
ground truth and is read exclusively by `evaluate.py` when scoring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .toy_service import run_service


@dataclass
class LogRecord:
    """A generated log on disk, paired with its hidden ground truth."""

    name: str
    log_path: Path
    meta_path: Path
    true_fault: str
    noise: bool
    request_count: int

    def text(self) -> str:
        return self.log_path.read_text(encoding="utf-8")

    def meta(self) -> dict:
        return json.loads(self.meta_path.read_text(encoding="utf-8"))


DEFAULT_REQUEST_COUNT = 30


def scenario_name(fault_type: str, run_id: int, noise: bool, request_count: int = DEFAULT_REQUEST_COUNT) -> str:
    suffix = "_noisy" if noise else ""
    length = "" if request_count == DEFAULT_REQUEST_COUNT else f"_r{request_count}"
    return f"{fault_type}_run{run_id}{suffix}{length}"


def generate_log(
    fault_type: str,
    run_id: int,
    logs_dir: Path,
    request_count: int = 30,
    noise: bool = False,
) -> LogRecord:
    """Generate one scenario and write both files. Overwrites deterministically."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    run = run_service(fault_type, run_id=run_id, request_count=request_count, noise=noise)

    name = scenario_name(fault_type, run_id, noise, request_count)
    log_path = logs_dir / f"{name}.log"
    meta_path = logs_dir / f"{name}.meta.json"

    log_path.write_text(run.render(), encoding="utf-8")
    meta_path.write_text(json.dumps(run.meta(), indent=2) + "\n", encoding="utf-8")

    return LogRecord(
        name=name,
        log_path=log_path,
        meta_path=meta_path,
        true_fault=fault_type,
        noise=noise,
        request_count=request_count,
    )


def generate_suite(
    logs_dir: Path,
    fault_types: list[str],
    runs_per_fault: int,
    request_count: int = 30,
    noise: bool = False,
) -> list[LogRecord]:
    """Generate `runs_per_fault` logs for each fault type -- the evaluation set."""
    records: list[LogRecord] = []
    for fault_type in fault_types:
        for run_id in range(1, runs_per_fault + 1):
            records.append(
                generate_log(
                    fault_type,
                    run_id,
                    logs_dir,
                    request_count=request_count,
                    noise=noise,
                )
            )
    return records


def discover_logs(logs_dir: Path) -> list[LogRecord]:
    """Find every generated log that still has its ground-truth sidecar."""
    records: list[LogRecord] = []
    if not logs_dir.exists():
        return records

    for log_path in sorted(logs_dir.glob("*.log")):
        meta_path = log_path.with_suffix(".meta.json")
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        records.append(
            LogRecord(
                name=log_path.stem,
                log_path=log_path,
                meta_path=meta_path,
                true_fault=meta.get("fault_type", "unknown"),
                noise=bool(meta.get("noise", False)),
                request_count=int(meta.get("request_count", 0)),
            )
        )
    return records
