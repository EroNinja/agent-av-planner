"""Fault taxonomy, runbook table, and the shared vocabulary of the system.

Everything that defines *what can go wrong* lives here so that adding a fault
type is a single-file change. The LLM never sees this module directly -- the
prompts in `agents/hypothesis_agent.py` are built from it.
"""

from __future__ import annotations

# Two faults at once. Used only by the compound dataset.
COMPOUND_FAULTS: list[str] = ["dependency_down+slow_query", "pool_exhausted+slow_query"]

# The fixed classification taxonomy. `no_fault` is the control label.
FAULT_TYPES: list[str] = [
    "dependency_down",
    "slow_query",
    "pool_exhausted",
    "no_fault",
]

FAULT_SCENARIOS: list[str] = [f for f in FAULT_TYPES if f != "no_fault"]

# Short descriptions handed to the model as the candidate set. These describe
# the *symptom shape*, not the injection mechanism -- the model has to match
# observed evidence against them.
FAULT_DESCRIPTIONS: dict[str, str] = {
    "dependency_down": (
        "A downstream dependency is unreachable. Requests fail with connection-level "
        "errors spread across the run rather than clustered in one window. Latency on "
        "surviving requests stays normal."
    ),
    "slow_query": (
        "Queries are executing slowly. Latency is elevated well above baseline across "
        "the run, but requests still succeed -- there are few or no error lines."
    ),
    "pool_exhausted": (
        "The connection pool ran out of connections. Failures arrive as a contiguous "
        "burst over a narrow range of consecutive request ids, then recover. Latency "
        "on surviving requests stays normal."
    ),
    "no_fault": (
        "Healthy run. No injected fault: errors are absent or limited to isolated, "
        "unrelated transients, and latency sits at baseline."
    ),
}

# Deterministic remediation lookup -- the Runbook Agent's entire implementation.
RUNBOOK: dict[str, str] = {
    "dependency_down": (
        "Check downstream dependency health and connectivity (e.g. `ping` or `telnet` to "
        "the dependency host and port). If confirmed down, fail over to a backup instance "
        "or escalate to the dependency's on-call owner."
    ),
    "slow_query": (
        "Inspect slow-query logs and `EXPLAIN` the hot statements. Look for a missing or "
        "unused index, a recent plan regression, or table bloat; add the index or force "
        "the prior plan, then re-measure p90 latency."
    ),
    "pool_exhausted": (
        "Check current connection pool usage against its configured limit. Raise the pool "
        "ceiling if the host can carry it, and audit for connection leaks -- borrowed "
        "connections that are never returned -- in recently deployed code paths."
    ),
    "no_fault": (
        "No remediation required. The run is within normal operating parameters; close "
        "the alert and record it as a non-incident."
    ),
}

# Recovery mode: what the model may propose, and which single plan cures each fault.
RECOVERY_ACTIONS: dict[str, str] = {
    "failover_dependency": "switch traffic to a standby instance of a dependency",
    "raise_pool_limit": "increase the maximum size of a connection pool",
    "add_index": "add a database index for the hot query",
    "restart_service": "restart a running service",
    "no_action": "make no change",
}
RECOVERY_TARGETS: dict[str, str] = {
    "db-primary": "the primary database dependency",
    "connection-pool": "the service's database connection pool",
    "query-engine": "the database query layer",
    "api-service": "the request-handling service",
    "none": "no component",
}
VALID_RECOVERY: dict[str, tuple[str, str]] = {
    "dependency_down": ("failover_dependency", "db-primary"),
    "pool_exhausted": ("raise_pool_limit", "connection-pool"),
    "slow_query": ("add_index", "query-engine"),
    "no_fault": ("no_action", "none"),
}

# Error strings the toy service emits. Kept here so the log correlator's
# expectations and the service's output can never drift apart.
ERROR_SIGNATURES: dict[str, tuple[str, str]] = {
    "dependency_down": (
        "ConnectionRefusedError",
        "Could not connect to downstream dependency at db.internal:5432",
    ),
    "pool_exhausted": (
        "PoolExhaustedError",
        "No available connections in pool (limit=5, active=6+ during burst)",
    ),
}

# In noise mode both error-producing faults emit this one signature. A distinct
# error name would identify the fault by itself, so with it removed the only
# remaining signal is how the failures are distributed across the run.
NOISY_ERROR_SIGNATURE: tuple[str, str] = (
    "OperationalError",
    "Could not obtain a database connection",
)

# Red-herring transients used only when noise is enabled.
TRANSIENT_SIGNATURES: list[tuple[str, str]] = [
    ("TimeoutError", "Upstream read timed out after 2000ms on /v1/profile"),
    ("ValidationError", "Malformed payload field 'locale' ignored"),
]

NOISE_MESSAGES: list[tuple[str, str]] = [
    ("cache", "cache hit rate degraded to 71% over last 60s"),
    ("gc", "GC pause 143ms exceeded soft target of 100ms"),
    ("config", "config reload applied: feature_flag.new_router=true"),
    ("tls", "certificate for metrics.internal expires in 14 days"),
    ("scheduler", "background compaction deferred, queue depth 3"),
]


def is_valid_fault(name: str) -> bool:
    return name in FAULT_TYPES


def remediation_for(fault_type: str) -> str:
    """The Runbook Agent. Deterministic dictionary lookup, no model involved."""
    return " ".join(RUNBOOK.get(part, RUNBOOK["no_fault"]) for part in fault_type.split("+"))
