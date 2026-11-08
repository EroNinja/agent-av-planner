"""SQLite persistence. Standard library only -- no ORM, no migrations to run.

Every diagnosis, interactive or from a sweep, lands in `runs`. That table is
the experiment's dataset: the comparison views in the dashboard are all just
aggregations over it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .failure_labels import label_run
from .grounding import assess
from .recovery import assess_recovery

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "runs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL,
    sweep_id         TEXT,
    log_name         TEXT    NOT NULL,
    true_fault       TEXT    NOT NULL,
    noise            INTEGER NOT NULL DEFAULT 0,
    condition        TEXT    NOT NULL,
    provider         TEXT    NOT NULL,
    model            TEXT    NOT NULL,
    predicted_fault  TEXT,
    correct          INTEGER,
    confidence       REAL,
    explanation      TEXT,
    remediation      TEXT,
    malformed        INTEGER NOT NULL DEFAULT 0,
    api_error        INTEGER NOT NULL DEFAULT 0,
    parse_error      TEXT,
    evidence_score   REAL,
    evidence_checked INTEGER,
    input_tokens     INTEGER DEFAULT 0,
    output_tokens    INTEGER DEFAULT 0,
    llm_latency_ms   REAL    DEFAULT 0,
    payload          TEXT,
    request_count    INTEGER,
    meets_proposal   INTEGER,
    grounded         INTEGER,
    recovery_action  TEXT,
    recovery_target  TEXT,
    recovery_valid   INTEGER,
    failure_labels   TEXT,
    llm_call_count   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_runs_sweep    ON runs(sweep_id);
CREATE INDEX IF NOT EXISTS idx_runs_model    ON runs(provider, model, condition);
CREATE INDEX IF NOT EXISTS idx_runs_created  ON runs(created_at DESC);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    for name, definition in (
        ("api_error", "INTEGER NOT NULL DEFAULT 0"),
        ("request_count", "INTEGER"),
        ("meets_proposal", "INTEGER"),
        ("grounded", "INTEGER"),
        ("recovery_action", "TEXT"),
        ("recovery_target", "TEXT"),
        ("recovery_valid", "INTEGER"),
        ("failure_labels", "TEXT"),
        ("llm_call_count", "INTEGER"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
    conn.commit()


def record_run(
    conn: sqlite3.Connection,
    diagnosis: dict,
    log_name: str,
    true_fault: str,
    noise: bool = False,
    sweep_id: str | None = None,
    meta: dict | None = None,
) -> int:
    predicted = diagnosis.get("predicted_fault")
    evidence = diagnosis.get("evidence") or {}
    meets_proposal, grounded = assess(true_fault, diagnosis, meta)
    recovery = assess_recovery(diagnosis, meta)
    labels = label_run(true_fault, diagnosis, meta, grounded, recovery)

    cursor = conn.execute(
        """
        INSERT INTO runs (
            created_at, sweep_id, log_name, true_fault, noise, condition, provider, model,
            predicted_fault, correct, confidence, explanation, remediation, malformed,
            api_error, parse_error, evidence_score, evidence_checked, input_tokens,
            output_tokens, llm_latency_ms, payload, request_count, meets_proposal, grounded,
            recovery_action, recovery_target, recovery_valid, failure_labels, llm_call_count
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            sweep_id,
            log_name,
            true_fault,
            int(noise),
            diagnosis.get("condition"),
            diagnosis.get("provider"),
            diagnosis.get("model"),
            predicted,
            None if predicted is None else int(predicted == true_fault),
            diagnosis.get("confidence"),
            diagnosis.get("explanation"),
            diagnosis.get("remediation"),
            int(bool(diagnosis.get("malformed"))),
            int(bool(diagnosis.get("api_error"))),
            diagnosis.get("parse_error"),
            evidence.get("score"),
            evidence.get("claims_checked"),
            diagnosis.get("input_tokens") or 0,
            diagnosis.get("output_tokens") or 0,
            diagnosis.get("llm_latency_ms") or 0.0,
            json.dumps(diagnosis),
            (meta or {}).get("request_count"),
            meets_proposal,
            grounded,
            recovery["action"] if recovery else None,
            recovery["target"] if recovery else None,
            None if recovery is None else int(recovery["valid"]),
            json.dumps(labels),
            diagnosis.get("llm_call_count"),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def list_runs(
    conn: sqlite3.Connection,
    limit: int = 200,
    provider: str | None = None,
    model: str | None = None,
    condition: str | None = None,
    sweep_id: str | None = None,
) -> list[dict]:
    clauses, params = [], []
    for column, value in (
        ("provider", provider),
        ("model", model),
        ("condition", condition),
        ("sweep_id", sweep_id),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, created_at, sweep_id, log_name, true_fault, noise, condition, provider,
               model, predicted_fault, correct, confidence, malformed, api_error, evidence_score,
               evidence_checked, input_tokens, output_tokens, llm_latency_ms, explanation,
               remediation, recovery_action, recovery_target, recovery_valid, failure_labels
        FROM runs {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    record = dict(row)
    if record.get("payload"):
        try:
            record["payload"] = json.loads(record["payload"])
        except json.JSONDecodeError:
            pass
    return record


def _scope(sweep_id: str | None, exclude_providers: list[str] | None) -> tuple[str, list]:
    """One sweep, or every sweep except non-comparable providers."""
    if sweep_id:
        return "WHERE sweep_id = ?", [sweep_id]
    clauses = ["sweep_id IS NOT NULL"]
    params: list = []
    if exclude_providers:
        marks = ",".join("?" for _ in exclude_providers)
        clauses.append(f"provider NOT IN ({marks})")
        params.extend(exclude_providers)
    return "WHERE " + " AND ".join(clauses), params


# Calls that never produced an answer are not evidence about the model, so they
# are left out of every denominator and reported separately as n_failed.
_ANSWERED = "NULLIF(SUM(1 - api_error), 0)"
_RECOVERED = f"SUM(CASE WHEN api_error = 0 AND correct = 1 THEN 1 ELSE 0 END) * 1.0 / {_ANSWERED}"
_STRICT = f"SUM(CASE WHEN api_error = 0 AND malformed = 0 AND correct = 1 THEN 1 ELSE 0 END) * 1.0 / {_ANSWERED}"


def aggregate(
    conn: sqlite3.Connection,
    sweep_id: str | None = None,
    exclude_providers: list[str] | None = None,
) -> list[dict]:
    """Accuracy and friends, one row per (provider, model, condition)."""
    where, params = _scope(sweep_id, exclude_providers)
    rows = conn.execute(
        f"""
        SELECT provider, model, condition,
               SUM(1 - api_error)                     AS n,
               SUM(api_error)                         AS n_failed,
               SUM(CASE WHEN api_error = 0 AND correct = 1 THEN 1 ELSE 0 END) AS n_correct,
               {_RECOVERED}                           AS accuracy,
               {_STRICT}                              AS strict_accuracy,
               AVG(confidence)                        AS mean_confidence,
               SUM(malformed) * 1.0 / {_ANSWERED}     AS malformed_rate,
               AVG(evidence_score)                    AS mean_evidence_score,
               SUM(input_tokens)                      AS input_tokens,
               SUM(output_tokens)                     AS output_tokens,
               AVG(llm_latency_ms)                    AS mean_latency_ms
        FROM runs {where}
        GROUP BY provider, model, condition
        ORDER BY provider, model, condition
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def aggregate_by_fault(
    conn: sqlite3.Connection,
    sweep_id: str | None = None,
    exclude_providers: list[str] | None = None,
) -> list[dict]:
    where, params = _scope(sweep_id, exclude_providers)
    rows = conn.execute(
        f"""
        SELECT provider, model, condition, true_fault,
               SUM(1 - api_error)  AS n,
               {_RECOVERED}        AS accuracy,
               {_STRICT}           AS strict_accuracy,
               AVG(confidence)     AS mean_confidence,
               SUM(CASE WHEN api_error = 0 AND predicted_fault IS NOT NULL
                         AND predicted_fault != 'no_fault' THEN 1 ELSE 0 END) * 1.0 / {_ANSWERED}
                                   AS fault_claim_rate
        FROM runs {where}
        GROUP BY provider, model, condition, true_fault
        ORDER BY provider, model, condition, true_fault
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def latest_sweep(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sweep_id FROM runs WHERE sweep_id IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["sweep_id"] if row else None


def list_sweeps(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT sweep_id,
               MIN(created_at) AS started_at,
               COUNT(*)        AS n_runs,
               COUNT(DISTINCT model || '|' || condition) AS n_arms,
               SUM(CASE WHEN api_error = 0 AND correct = 1 THEN 1 ELSE 0 END) * 1.0
                 / NULLIF(SUM(1 - api_error), 0) AS accuracy
        FROM runs
        WHERE sweep_id IS NOT NULL
        GROUP BY sweep_id
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
