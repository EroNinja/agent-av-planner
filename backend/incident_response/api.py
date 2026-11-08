"""FastAPI backend.

Wraps the same core package the CLI uses -- `diagnose.py`, `run_pipeline.py`
and `evaluate.py` import from here, so the web tier can never drift from the
graded command-line artifacts.

Two streaming surfaces:
  * `/api/diagnose/stream` runs one diagnosis and emits a trace event per agent
    step, so the dashboard can show the pipeline firing in real time.
  * `/api/sweep/{id}/stream` follows a sweep running on a background thread,
    replaying from the beginning so a reconnecting client misses nothing.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from . import db
from .analysis import analyze
from .conditions import ALL_CONDITIONS, CONDITIONS, diagnose_stream
from .evaluation import Arm, plan_size, run_sweep_stream, validate_arms
from .key_store import KeyStore, KeyStoreError
from .log_generator import discover_logs, generate_log, generate_suite
from .providers import PROVIDERS, get_client, provider_catalog, verify_key
from .real_logs import MAX_LOG_BYTES, diagnose_real, preview as preview_real_log
from .recovery import assess_recovery
from .providers.base import ProviderError
from .taxonomy import FAULT_DESCRIPTIONS, FAULT_TYPES, RUNBOOK

load_dotenv()

BACKEND_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BACKEND_DIR / "logs"
DB_PATH = BACKEND_DIR / "data" / "runs.db"

app = FastAPI(title="Incident Response", version="1.0.0")

# The key endpoints can change credentials, so only local hostnames are served.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_conn = db.connect(DB_PATH)
db.init_db(_conn)

_keys = KeyStore(
    BACKEND_DIR / ".env",
    BACKEND_DIR / "data" / "keys.json",
    {name: spec["env_key"] for name, spec in PROVIDERS.items() if spec["env_key"]},
)


# --------------------------------------------------------------------------
# Server-sent events
# --------------------------------------------------------------------------

def sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    fault_type: str | None = None
    run_id: int = 1
    suite: bool = False
    runs_per_fault: int = 2
    requests: int = Field(default=30, ge=5, le=400)
    noise: bool = False


class DiagnoseRequest(BaseModel):
    log_name: str
    condition: str = "pipeline"
    provider: str = "mock"
    model: str | None = None
    recovery: bool = False


class RealLogRequest(BaseModel):
    text: str
    provider: str = "mock"
    model: str | None = None


class AddKeyRequest(BaseModel):
    provider: str
    label: str = ""
    value: str
    activate: bool = True


class SweepRequest(BaseModel):
    arms: list[str] = Field(default_factory=list, description="provider:model strings")
    conditions: list[str] = Field(default_factory=lambda: list(CONDITIONS))
    log_names: list[str] | None = None
    recovery: bool = False
    hint: bool = True


# --------------------------------------------------------------------------
# Basics
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "logs_dir": str(LOGS_DIR),
        "db": str(DB_PATH),
    }


@app.get("/api/taxonomy")
def taxonomy() -> dict:
    return {
        "fault_types": FAULT_TYPES,
        "descriptions": FAULT_DESCRIPTIONS,
        "runbook": RUNBOOK,
        "conditions": [
            {"id": "single_agent", "label": "Single-agent baseline",
             "steps": ["hypothesis_agent (LLM)"]},
            {"id": "pipeline", "label": "Multi-agent pipeline",
             "steps": ["log_correlator (deterministic)", "hypothesis_agent (LLM)",
                       "runbook_agent (deterministic)"]},
        ],
    }


@app.get("/api/providers")
def providers(live: bool = Query(default=False)) -> dict:
    return {"providers": provider_catalog(live=live)}


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------

def _keys_response() -> dict:
    listing = _keys.listing()
    return {
        "providers": [
            {"provider": name, "label": PROVIDERS[name]["label"], "keys": entries}
            for name, entries in listing.items()
        ]
    }


def _key_operation(action) -> dict:
    try:
        action()
        return _keys_response()
    except KeyStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/keys")
def list_keys() -> dict:
    try:
        return _keys_response()
    except KeyStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/keys")
def add_key(request: AddKeyRequest) -> dict:
    return _key_operation(
        lambda: _keys.add(request.provider, request.label, request.value, request.activate)
    )


@app.post("/api/keys/{provider}/{key_id}/activate")
def activate_key(provider: str, key_id: str) -> dict:
    return _key_operation(lambda: _keys.activate(provider, key_id))


@app.delete("/api/keys/{provider}/{key_id}")
def remove_key(provider: str, key_id: str) -> dict:
    return _key_operation(lambda: _keys.remove(provider, key_id))


@app.post("/api/keys/{provider}/{key_id}/test")
def test_key(provider: str, key_id: str) -> dict:
    try:
        value = _keys.value_of(provider, key_id)
    except KeyStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    accepted, message = verify_key(provider, value)
    return {"ok": accepted, "message": message}


# --------------------------------------------------------------------------
# Real logs (advisory, never stored)
# --------------------------------------------------------------------------

def _check_real_log(text: str) -> None:
    if not text.strip():
        raise HTTPException(status_code=400, detail="the log is empty")
    if len(text.encode("utf-8")) > MAX_LOG_BYTES:
        raise HTTPException(status_code=413, detail="the log is larger than 5 MB")


@app.post("/api/real-log/preview")
def real_log_preview(request: RealLogRequest) -> dict:
    _check_real_log(request.text)
    return preview_real_log(request.text)


@app.post("/api/real-log/diagnose")
def real_log_diagnose(request: RealLogRequest) -> dict:
    _check_real_log(request.text)
    try:
        client = get_client(request.provider, request.model)
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return diagnose_real(request.text, client)


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------

def _log_payload(record) -> dict:
    return {
        "name": record.name,
        "true_fault": record.true_fault,
        "noise": record.noise,
        "request_count": record.request_count,
    }


@app.get("/api/logs")
def list_logs() -> dict:
    return {"logs": [_log_payload(record) for record in discover_logs(LOGS_DIR)]}


@app.get("/api/logs/{name}")
def get_log(name: str) -> dict:
    for record in discover_logs(LOGS_DIR):
        if record.name == name:
            return {**_log_payload(record), "text": record.text(), "meta": record.meta()}
    raise HTTPException(status_code=404, detail=f"no log named '{name}'")


@app.post("/api/logs/generate")
def generate(request: GenerateRequest) -> dict:
    if request.suite:
        records = generate_suite(
            LOGS_DIR, FAULT_TYPES, request.runs_per_fault,
            request_count=request.requests, noise=request.noise,
        )
    else:
        if request.fault_type not in FAULT_TYPES:
            raise HTTPException(status_code=400, detail=f"unknown fault type '{request.fault_type}'")
        records = [
            generate_log(
                request.fault_type, request.run_id, LOGS_DIR,
                request_count=request.requests, noise=request.noise,
            )
        ]
    return {"generated": [_log_payload(record) for record in records]}


# --------------------------------------------------------------------------
# Single diagnosis
# --------------------------------------------------------------------------

def _resolve_log(name: str):
    for record in discover_logs(LOGS_DIR):
        if record.name == name:
            return record
    raise HTTPException(status_code=404, detail=f"no log named '{name}'")


@app.post("/api/diagnose/stream")
def diagnose_streaming(request: DiagnoseRequest) -> StreamingResponse:
    if request.condition not in ALL_CONDITIONS:
        raise HTTPException(status_code=400, detail=f"unknown condition '{request.condition}'")

    record = _resolve_log(request.log_name)
    try:
        client = get_client(request.provider, request.model)
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_text = record.text()

    def events() -> Iterator[str]:
        try:
            for event in diagnose_stream(log_text, client, request.condition, recovery=request.recovery):
                if event["type"] == "result":
                    result = event["result"]
                    run_id = db.record_run(
                        _conn, result,
                        log_name=record.name,
                        true_fault=record.true_fault,
                        noise=record.noise,
                        meta=record.meta(),
                    )
                    event["run_id"] = run_id
                    event["true_fault"] = record.true_fault
                    event["correct"] = result.get("predicted_fault") == record.true_fault
                    event["recovery_result"] = assess_recovery(result, record.meta())
                yield sse(event)
        except Exception as exc:  # noqa: BLE001 - must reach the browser as an event
            yield sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


# --------------------------------------------------------------------------
# Sweeps (background thread + replayable event stream)
# --------------------------------------------------------------------------

class SweepJob:
    """A sweep executing off the request thread, with a replayable event log."""

    def __init__(self, sweep_id: str, total: int):
        self.sweep_id = sweep_id
        self.total = total
        self.events: list[dict] = []
        self.finished = threading.Event()
        self.error: str | None = None
        self._lock = threading.Lock()
        self._wakeup: queue.Queue[None] = queue.Queue()

    def append(self, event: dict) -> None:
        with self._lock:
            self.events.append(event)
        try:
            self._wakeup.put_nowait(None)
        except queue.Full:  # pragma: no cover
            pass

    def snapshot(self, start: int) -> list[dict]:
        with self._lock:
            return self.events[start:]

    def wait_for_event(self, timeout: float = 1.0) -> None:
        try:
            self._wakeup.get(timeout=timeout)
        except queue.Empty:
            pass


_sweeps: dict[str, SweepJob] = {}
_sweeps_lock = threading.Lock()


def _run_sweep_job(job: SweepJob, records, arms, conditions, recovery: bool = False, hint: bool = True) -> None:
    conn = db.connect(DB_PATH)
    try:
        for event in run_sweep_stream(
            records, arms, conn, conditions=conditions, sweep_id=job.sweep_id, recovery=recovery, hint=hint
        ):
            job.append(event)
    except Exception as exc:  # noqa: BLE001
        job.error = f"{type(exc).__name__}: {exc}"
        job.append({"type": "error", "message": job.error})
    finally:
        conn.close()
        job.finished.set()
        job.append({"type": "stream_end"})


@app.post("/api/sweep")
def start_sweep(request: SweepRequest) -> dict:
    records = discover_logs(LOGS_DIR)
    if request.log_names:
        wanted = set(request.log_names)
        records = [record for record in records if record.name in wanted]
    if not records:
        raise HTTPException(status_code=400, detail="no logs to evaluate -- generate some first")

    conditions = request.conditions or list(CONDITIONS)
    for condition in conditions:
        if condition not in ALL_CONDITIONS:
            raise HTTPException(status_code=400, detail=f"unknown condition '{condition}'")

    arms: list[Arm] = []
    for token in request.arms or ["mock:"]:
        provider, _, model = token.partition(":")
        if provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail=f"unknown provider '{provider}'")
        arms.append(Arm(provider=provider, model=model or PROVIDERS[provider]["default_model"]))

    try:
        validate_arms(arms)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sweep_id = uuid.uuid4().hex[:12]
    job = SweepJob(sweep_id, plan_size(len(records), arms, conditions))
    with _sweeps_lock:
        _sweeps[sweep_id] = job

    thread = threading.Thread(
        target=_run_sweep_job, args=(job, records, arms, conditions, request.recovery, request.hint), daemon=True
    )
    thread.start()

    return {
        "sweep_id": sweep_id,
        "total": job.total,
        "n_logs": len(records),
        "arms": [arm.key for arm in arms],
        "conditions": conditions,
    }


@app.get("/api/sweep/{sweep_id}/stream")
def stream_sweep(sweep_id: str) -> StreamingResponse:
    with _sweeps_lock:
        job = _sweeps.get(sweep_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no sweep '{sweep_id}'")

    def events() -> Iterator[str]:
        cursor = 0
        while True:
            batch = job.snapshot(cursor)
            cursor += len(batch)
            for event in batch:
                if event.get("type") == "stream_end":
                    return
                yield sse(event)
            if job.finished.is_set() and cursor >= len(job.events):
                return
            job.wait_for_event(timeout=0.5)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/sweeps")
def sweeps() -> dict:
    return {"sweeps": db.list_sweeps(_conn)}


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@app.get("/api/runs")
def runs(
    limit: int = Query(default=200, ge=1, le=2000),
    provider: str | None = None,
    model: str | None = None,
    condition: str | None = None,
    sweep_id: str | None = None,
) -> dict:
    return {
        "runs": db.list_runs(
            _conn, limit=limit, provider=provider, model=model,
            condition=condition, sweep_id=sweep_id,
        )
    }


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> dict:
    record = db.get_run(_conn, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    return record


@app.get("/api/analysis")
def analysis(sweep_ids: str | None = None) -> dict:
    """Paired statistics for the given comma-separated sweeps, or the latest one."""
    wanted = [item.strip() for item in (sweep_ids or "").split(",") if item.strip()]
    if not wanted:
        latest = db.latest_sweep(_conn)
        wanted = [latest] if latest else []
    return analyze(_conn, wanted)


@app.get("/api/stats")
def stats(sweep_id: str | None = None, pooled: bool = Query(default=False)) -> dict:
    """One sweep's results: the given one, or the latest. `pooled` spans all sweeps."""
    if sweep_id is None and not pooled:
        sweep_id = db.latest_sweep(_conn)
    excluded = [name for name, spec in PROVIDERS.items() if not spec["comparable"]]
    by_arm = db.aggregate(_conn, sweep_id=sweep_id, exclude_providers=excluded)
    by_fault = db.aggregate_by_fault(_conn, sweep_id=sweep_id, exclude_providers=excluded)

    # False-positive rate: how often a healthy run is called a fault.
    false_positives = {}
    for row in by_fault:
        if row["true_fault"] != "no_fault":
            continue
        key = f"{row['provider']}:{row['model']}|{row['condition']}"
        false_positives[key] = round(row["fault_claim_rate"] or 0.0, 4)

    # The headline: pipeline accuracy minus baseline accuracy, per model.
    deltas = []
    arms = {(row["provider"], row["model"]) for row in by_arm}
    for provider, model in sorted(arms):
        rows = {row["condition"]: row for row in by_arm
                if row["provider"] == provider and row["model"] == model}
        single, pipeline = rows.get("single_agent"), rows.get("pipeline")
        if not single or not pipeline:
            continue
        deltas.append(
            {
                "provider": provider,
                "model": model,
                "single_agent_accuracy": single["accuracy"],
                "pipeline_accuracy": pipeline["accuracy"],
                "delta": round((pipeline["accuracy"] or 0) - (single["accuracy"] or 0), 4),
                "strict_delta": round(
                    (pipeline["strict_accuracy"] or 0) - (single["strict_accuracy"] or 0), 4
                ),
                "n_per_condition": single["n"],
            }
        )

    return {
        "sweep_id": sweep_id,
        "pooled": pooled,
        "by_arm": by_arm,
        "by_fault": by_fault,
        "false_positive_rate": false_positives,
        "deltas": deltas,
    }
