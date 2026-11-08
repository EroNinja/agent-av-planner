"""First-pass diagnosis of real logs. Advisory only.

Secrets and personal data are removed before anything else happens, and the model
receives a summary plus a small sample of lines, never the whole file. Nothing
here is stored or scored, because real logs have no ground truth, so this mode
provides no evidence for the controlled experiment.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from .agents.log_correlator import _percentile
from .providers.base import extract_json
from .taxonomy import FAULT_DESCRIPTIONS

MAX_LOG_BYTES = 5_000_000
MAX_SAMPLE_LINES = 40
MAX_LINE_CHARS = 300
ABSTAIN_BELOW = 0.6

REDACTIONS: list[tuple[str, re.Pattern, str]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED_PRIVATE_KEY]"),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED_TOKEN]"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED_JWT]"),
    ("api_key", re.compile(r"\b(?:gsk_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"), "[REDACTED_API_KEY]"),
    (
        "secret_field",
        re.compile(r"(?i)\b((?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)[\"']?\s*[=:]\s*[\"']?)[^\s\"',;&]+"),
        r"\1[REDACTED]",
    ),
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED_NUMBER]"),
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[REDACTED_IP]"),
]


def _luhn(number: str) -> bool:
    digits = [int(ch) for ch in number if ch.isdigit()][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Remove secrets and personal data. Returns the clean text and counts by kind."""
    counts: dict[str, int] = {}
    for name, pattern, replacement in REDACTIONS:
        if name == "card_number":
            def replace(match: re.Match) -> str:
                if _luhn(match.group(0)):
                    counts[name] = counts.get(name, 0) + 1
                    return replacement
                return match.group(0)

            text = pattern.sub(replace, text)
        else:
            text, found = pattern.subn(replacement, text)
            if found:
                counts[name] = counts.get(name, 0) + found
    return text, counts


KV_RE = re.compile(r"""([A-Za-z_][\w.-]*)=("[^"]*"|'[^']*'|\S+)""")
LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRITICAL|CRIT|FATAL|PANIC)\b", re.I)
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|\b[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}")
LATENCY_TEXT_RE = re.compile(r"(?i)\b(?:latency|duration|elapsed|took|response[_ ]?time)\D{0,12}(\d+(?:\.\d+)?)\s*(ms|s)?\b")
ERROR_NAME_RE = re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception|Timeout)\b")
LEVEL_KEYS = ("level", "lvl", "severity", "loglevel")
MESSAGE_KEYS = ("message", "msg", "error", "event")
ERROR_KEYS = ("error_type", "exception", "error", "err")
LATENCY_KEYS = ("latency_ms", "duration_ms", "elapsed_ms", "response_time_ms", "latency", "duration", "elapsed", "response_time")
STATUS_KEYS = ("status", "status_code", "http_status", "code")
ERROR_LEVELS = {"ERROR", "ERR", "CRITICAL", "CRIT", "FATAL", "PANIC"}


def _normalize_level(value) -> str | None:
    text = str(value).upper() if value is not None else ""
    return {"WARNING": "WARN"}.get(text, text) or None


def _first(fields: dict, keys: tuple[str, ...]):
    lowered = {str(key).lower(): value for key, value in fields.items()}
    return next((lowered[key] for key in keys if lowered.get(key) not in (None, "")), None)


def parse_line(line: str) -> dict:
    """Read one line of JSON, key=value or plain text into common fields."""
    stripped = line.strip()
    fields: dict = {}
    shape = "plain"
    if stripped.startswith("{"):
        try:
            loaded = json.loads(stripped)
            if isinstance(loaded, dict):
                fields, shape = loaded, "json"
        except json.JSONDecodeError:
            pass
    if shape == "plain":
        pairs = KV_RE.findall(stripped)
        if len(pairs) >= 2:
            fields = {key: value.strip("\"'") for key, value in pairs}
            shape = "key_value"

    level = _normalize_level(_first(fields, LEVEL_KEYS))
    if level is None:
        match = LEVEL_RE.search(stripped)
        level = _normalize_level(match.group(1)) if match else None

    latency = None
    raw_latency = _first(fields, LATENCY_KEYS)
    if raw_latency is not None:
        try:
            latency = float(str(raw_latency).rstrip("ms").rstrip())
        except ValueError:
            latency = None
    if latency is None:
        match = LATENCY_TEXT_RE.search(stripped)
        if match:
            latency = float(match.group(1)) * (1000 if (match.group(2) or "").lower() == "s" else 1)

    status = _first(fields, STATUS_KEYS)
    is_error = level in ERROR_LEVELS or str(status).lower() == "error" or (str(status).isdigit() and int(str(status)) >= 500)
    error_name = _first(fields, ERROR_KEYS)
    if error_name is None:
        match = ERROR_NAME_RE.search(stripped)
        error_name = match.group(0) if match else None
    message = _first(fields, MESSAGE_KEYS)
    return {
        "shape": shape,
        "level": level,
        "is_error": bool(is_error),
        "latency_ms": latency,
        "error_name": str(error_name)[:80] if error_name else None,
        "message": str(message) if message else stripped,
        "raw": stripped,
    }


def _signature(message: str) -> str:
    """Collapse the variable parts of a message so similar errors group together."""
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27}\b", "<id>", message, flags=re.I)
    text = re.sub(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", "<hex>", text, flags=re.I)
    text = re.sub(r"\d+(?:\.\d+)?", "<n>", text)
    return text[:160]


def summarize(text: str) -> dict:
    lines = [line for line in text.splitlines() if line.strip()]
    parsed = [parse_line(line) for line in lines]
    errors = [(index, row) for index, row in enumerate(parsed) if row["is_error"]]
    latencies = [row["latency_ms"] for row in parsed if row["latency_ms"] is not None]
    signatures = Counter(_signature(row["error_name"] or row["message"]) for _, row in errors)

    buckets = [0] * 10
    for index, _ in errors:
        buckets[min(9, index * 10 // max(1, len(parsed)))] += 1

    times = [match.group(0) for line in lines[:1] + lines[-1:] if (match := TIME_RE.search(line))]
    return {
        "total_lines": len(parsed),
        "line_formats": dict(Counter(row["shape"] for row in parsed)),
        "levels": dict(Counter(row["level"] or "none" for row in parsed)),
        "error_lines": len(errors),
        "top_error_signatures": signatures.most_common(8),
        "errors_by_tenth_of_file": buckets,
        "first_and_last_timestamp": times,
        "latency_ms": {
            "count": len(latencies),
            "mean": round(sum(latencies) / len(latencies), 1),
            "p90": round(_percentile(latencies, 90), 1),
            "max": round(max(latencies), 1),
        }
        if latencies
        else None,
    }


def sample_lines(text: str) -> list[str]:
    """Error lines first, spread across the file, then a little context."""
    lines = [line.strip()[:MAX_LINE_CHARS] for line in text.splitlines() if line.strip()]
    flagged = [i for i, line in enumerate(lines) if parse_line(line)["is_error"]]
    picked = set(flagged[:: max(1, len(flagged) // (MAX_SAMPLE_LINES - 8))][: MAX_SAMPLE_LINES - 8])
    step = max(1, len(lines) // 8)
    picked.update(range(0, len(lines), step))
    return [lines[i] for i in sorted(picked)[:MAX_SAMPLE_LINES]]


OPEN_PROMPT = """You help an on-call engineer with a first-pass diagnosis of a service log. \
You see a summary and a sample of lines, not the whole log, and sensitive values were removed.

Known patterns you may use as labels, or use `unknown`:
{taxonomy}

Rules:
- More than one cause can be present. If the evidence is weak or does not fit, say `unknown`.
- Quote evidence exactly as it appears in the sample lines. Never invent a line or a value.
- This is advisory. Suggest checks for a person to run. Do not claim anything has been fixed.

Reply with one JSON object and nothing else:
{{
  "hypotheses": [{{"label": "<label or unknown>", "cause": "<one sentence>", "confidence": <0-1>, "evidence": ["<exact quote>"]}}],
  "next_checks": ["<a read-only check a person should run>"]
}}
List hypotheses from most to least likely."""


def _prompt() -> str:
    taxonomy = "\n".join(f"- {name}: {text}" for name, text in FAULT_DESCRIPTIONS.items())
    return OPEN_PROMPT.format(taxonomy=taxonomy)


def preview(raw_text: str) -> dict:
    clean, counts = redact(raw_text)
    return {"redactions": counts, "summary": summarize(clean), "sample": sample_lines(clean)}


def diagnose_real(raw_text: str, client) -> dict:
    clean, counts = redact(raw_text)
    summary = summarize(clean)
    sample = sample_lines(clean)
    payload = f"Summary:\n{json.dumps(summary, indent=2)}\n\nSample lines:\n" + "\n".join(sample)
    response = client.complete(_prompt(), payload, max_tokens=2048)
    result = {"redactions": counts, "summary": summary, "provider": client.provider, "model": client.model}
    if not response.ok:
        return {**result, "error": response.error}

    parsed, error = extract_json(response.text)
    if parsed is None:
        return {**result, "error": error}

    hypotheses = parsed.get("hypotheses")
    if not isinstance(hypotheses, list) and parsed.get("predicted_fault"):
        hypotheses = [{"label": parsed["predicted_fault"], "cause": parsed.get("explanation", ""), "confidence": parsed.get("confidence"), "evidence": []}]

    cleaned = []
    for item in hypotheses if isinstance(hypotheses, list) else []:
        if not isinstance(item, dict):
            continue
        quotes = [str(q) for q in item.get("evidence") or [] if isinstance(q, (str, int, float))]
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence"))))
        except (TypeError, ValueError):
            confidence = None
        cleaned.append(
            {
                "label": str(item.get("label", "unknown")),
                "cause": str(item.get("cause", ""))[:500],
                "confidence": confidence,
                "evidence": [{"quote": q[:300], "found_in_log": q in clean} for q in quotes[:6]],
            }
        )

    top = cleaned[0] if cleaned else None
    needs_human = (
        top is None
        or top["confidence"] is None
        or top["confidence"] < ABSTAIN_BELOW
        or top["label"] == "unknown"
        or (bool(top["evidence"]) and not all(e["found_in_log"] for e in top["evidence"]))
    )
    checks = parsed.get("next_checks")
    return {
        **result,
        "hypotheses": cleaned,
        "next_checks": [str(c)[:300] for c in checks[:6]] if isinstance(checks, list) else [],
        "needs_human": needs_human,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
    }
