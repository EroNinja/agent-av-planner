"""A self-contained toy service with deterministic, request-id-keyed fault injection.

There is no HTTP server and no database. `run_service()` is a pure function of
(fault_type, run_id, request_count, noise): the same arguments always produce
byte-identical logs, which is what makes the evaluation reproducible.

Every fault is injected by a predicate over the request id, so ground truth is
exactly known at generation time and is written to a sibling `.meta.json` that
the model never sees.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .taxonomy import ERROR_SIGNATURES, NOISE_MESSAGES, NOISY_ERROR_SIGNATURE, TRANSIENT_SIGNATURES

BASELINE_LATENCY_MEAN = 45.0
BASELINE_LATENCY_SD = 9.0
SLOW_QUERY_LATENCY_MEAN = 310.0
SLOW_QUERY_LATENCY_SD = 55.0
POOL_BURST_LENGTH = 5

EPOCH = datetime(2026, 3, 14, 9, 15, 0, tzinfo=timezone.utc)


@dataclass
class LogEvent:
    """One line of the emitted log."""

    timestamp: datetime
    level: str
    request_id: int | None = None
    status: str | None = None
    latency_ms: float | None = None
    error_type: str | None = None
    message: str | None = None
    component: str | None = None

    def render(self) -> str:
        ts = self.timestamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.timestamp.microsecond // 1000:03d}Z"
        parts = [ts, f"level={self.level}"]
        if self.component is not None:
            parts.append(f"component={self.component}")
        if self.request_id is not None:
            parts.append(f"request_id={self.request_id}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.latency_ms is not None:
            parts.append(f"latency_ms={self.latency_ms:.2f}")
        if self.error_type is not None:
            parts.append(f"error_type={self.error_type}")
        if self.message is not None:
            parts.append(f'message="{self.message}"')
        return " ".join(parts)


@dataclass
class ServiceRun:
    fault_type: str
    run_id: int
    request_count: int
    noise: bool
    seed: int
    events: list[LogEvent] = field(default_factory=list)
    injected_request_ids: list[int] = field(default_factory=list)
    red_herring_request_ids: list[int] = field(default_factory=list)

    def render(self) -> str:
        return "\n".join(event.render() for event in self.events) + "\n"

    def meta(self) -> dict:
        """Ground truth. Written to .meta.json, never shown to any model."""
        return {
            "fault_type": self.fault_type,
            "run_id": self.run_id,
            "seed": self.seed,
            "request_count": self.request_count,
            "noise": self.noise,
            "injected_request_ids": self.injected_request_ids,
            "red_herring_request_ids": self.red_herring_request_ids,
        }


def _seed_for(fault_type: str, run_id: int, noise: bool) -> int:
    """Stable seed derived from the scenario, so runs are reproducible by name.

    Uses a cryptographic digest rather than `hash()`: CPython randomises string
    hashing per process (PYTHONHASHSEED), which would make "the same scenario"
    produce different logs on every invocation and quietly destroy the
    reproducibility the whole evaluation rests on.
    """
    key = f"{fault_type}|{run_id}|{int(noise)}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(key).digest()[:4], "big")


def _failing_ids(fault_type: str, request_count: int, rng: random.Random, noise: bool) -> list[int]:
    """Which request ids the injected fault causes to fail."""
    if fault_type == "dependency_down":
        if not noise:
            # Perfectly regular: every third request.
            return [rid for rid in range(request_count) if rid % 3 == 0]
        # Noisy variant: irregular gaps of 2-4, so the cadence is no longer a
        # giveaway and has to be inferred as "spread out, not clustered".
        ids, rid = [], rng.randint(0, 2)
        while rid < request_count:
            ids.append(rid)
            rid += rng.randint(2, 4)
        return ids
    if fault_type == "pool_exhausted":
        latest_start = max(1, request_count - POOL_BURST_LENGTH - 1)
        start = rng.randint(1, latest_start)
        length = POOL_BURST_LENGTH if not noise else rng.randint(4, 7)
        return [rid for rid in range(start, min(start + length, request_count))]
    return []


def run_service(
    fault_type: str,
    run_id: int = 1,
    request_count: int = 30,
    noise: bool = False,
) -> ServiceRun:
    """Simulate one run of the toy service and return its log events + ground truth."""
    seed = _seed_for(fault_type, run_id, noise)
    rng = random.Random(seed)

    failing = _failing_ids(fault_type, request_count, rng, noise)
    run = ServiceRun(
        fault_type=fault_type,
        run_id=run_id,
        request_count=request_count,
        noise=noise,
        seed=seed,
        injected_request_ids=list(failing),
    )

    # A red herring is an isolated, unrelated transient failure. It exists only
    # in noise mode and is deliberately NOT the injected fault -- it is there to
    # punish a model that classifies on "an error appeared" alone.
    red_herrings: set[int] = set()
    if noise:
        candidates = [rid for rid in range(request_count) if rid not in set(failing)]
        if candidates:
            for rid in rng.sample(candidates, k=min(len(candidates), rng.randint(0, 2))):
                red_herrings.add(rid)
    run.red_herring_request_ids = sorted(red_herrings)

    latency_mean = SLOW_QUERY_LATENCY_MEAN if fault_type == "slow_query" else BASELINE_LATENCY_MEAN
    latency_sd = SLOW_QUERY_LATENCY_SD if fault_type == "slow_query" else BASELINE_LATENCY_SD
    if noise:
        latency_sd *= 1.6  # wider spread makes the latency read less clean

    clock = EPOCH
    for rid in range(request_count):
        latency = max(1.0, rng.gauss(latency_mean, latency_sd))
        clock += timedelta(milliseconds=rng.randint(40, 180))

        if rid in failing:
            error_type, message = NOISY_ERROR_SIGNATURE if noise else ERROR_SIGNATURES[fault_type]
            run.events.append(
                LogEvent(
                    timestamp=clock,
                    level="ERROR",
                    request_id=rid,
                    status="error",
                    latency_ms=latency,
                    error_type=error_type,
                    message=message,
                )
            )
        elif rid in red_herrings:
            error_type, message = rng.choice(TRANSIENT_SIGNATURES)
            run.events.append(
                LogEvent(
                    timestamp=clock,
                    level="ERROR",
                    request_id=rid,
                    status="error",
                    latency_ms=latency,
                    error_type=error_type,
                    message=message,
                )
            )
        else:
            run.events.append(
                LogEvent(
                    timestamp=clock,
                    level="INFO",
                    request_id=rid,
                    status="ok",
                    latency_ms=latency,
                )
            )

        # Interleave unrelated operational chatter.
        if noise and rng.random() < 0.22:
            component, message = rng.choice(NOISE_MESSAGES)
            clock += timedelta(milliseconds=rng.randint(5, 40))
            run.events.append(
                LogEvent(timestamp=clock, level="WARN", component=component, message=message)
            )

    return run
