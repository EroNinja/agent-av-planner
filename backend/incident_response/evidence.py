"""Automatic evidence-citation scoring.

An explanation is only worth anything if the evidence it cites exists in the
input. Three claim types are extracted from the explanation text:

  * error-type names -- must appear in the log.
  * request ids      -- "request_id=1,4,8", "requests 10-14", "ids 3, 6, 9".
                        A range counts its two endpoints only, and a citation
                        whose sentence makes no claim about it is ignored.
                        A plain citation is a claim that the request failed; a
                        citation described as succeeding is a claim that it did
                        not.
  * latency figures  -- must fall inside the observed latency range, with a
                        tolerance band, or appear literally in the log.
                        Thresholds such as "under 100 ms" are comparisons, not
                        measurements, and are skipped.

Anything the extractor cannot confidently identify is not counted.
`claims_checked == 0` means no verifiable claim was made, reported as `None`
rather than a perfect score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ERROR_TYPE_RE = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Exception))\b")

_DASH = r"(?:-|\u2013|\u2011|\bto\b|\bthrough\b)"
# A request id is a whole number, not the start of 33.26 or of 57 ms.
_ID = r"\d+(?!\d|\.\d|/\d|\s*ms\b)"
ID_MENTION_RE = re.compile(
    r"\b(?:error_)?(?:request[_\s-]?ids?|requests?|ids?)\s*[#=:]?\s*(?:\(\s*)?(?:e\.g\.,?\s*)?"
    r"(" + _ID + r"(?:\s*(?:,|" + _DASH + r")\s*" + _ID + r")*)",
    re.IGNORECASE,
)
RANGE_SPLIT_RE = re.compile(r"\s*" + _DASH + r"\s*", re.IGNORECASE)
SUCCESS_BEFORE_RE = re.compile(
    r"(?:(?:success\w*|ok|healthy|normal|surviving)\s+(?:requests?\s*)?\(?\s*(?:e\.g\.,?\s*)?"
    r"|(?:recover\w*|stop\w*|end\w*)\s+(?:at|by|on|from|with)\s+)$",
    re.IGNORECASE,
)
# "24 ms at id 0" and "request_id=5 latency_ms=45" locate a measurement. They
# say nothing about whether the request failed.
NEUTRAL_BEFORE_RE = re.compile(
    r"(?:(?:\bms|latenc\w*)\s*(?:at|for|on|in)\s+(?:the\s+)?"
    r"|\b(?:surrounding|subsequent|remaining|other|following|preceding|later|earlier)\s+)$",
    re.IGNORECASE,
)
NEUTRAL_AFTER_RE = re.compile(
    r"^\s*[:,(=]?\s*(?:with\s+|had\s+|has\s+|at\s+)?(?:latency|took\b|\d+(?:\.\d+)?\s*ms)",
    re.IGNORECASE,
)
SUCCESS_AFTER_RE = re.compile(
    r"^\s*\)?\s*(?:(?:all|were|was|are|is|have|has)\s+)*"
    r"(?:then\s+)?(?:succe\w*|resume\w*|ok\b|status=ok|healthy|normal|completed|did not fail|had no error)",
    re.IGNORECASE,
)

FAILURE_WORD_RE = re.compile(
    r"\w*(?:error|exception|timeout)\w*|\bfail\w*|\bburst\w*|refused\b|\btimed\s+out\b|\bexhaust\w*|\bunreachable\b",
    re.IGNORECASE,
)
SENTENCE_BREAK_RE = re.compile(r"(?<!\d)(?<!e\.g)(?<!i\.e)\.(?=\s|$)|;")
FAILURE_LOOKAHEAD = 80
SPACING_RE = re.compile(r"mean[_\s]gaps?|contiguous[_\s]runs?|gaps?\s+(?:of|ranging|up)", re.IGNORECASE)
LATENCY_FIELD_RE = re.compile(r"latenc\w*[^;]{0,80}?(\d+\.\d+)", re.IGNORECASE)

LATENCY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ms\b", re.IGNORECASE)
LOG_LATENCY_RE = re.compile(r"latency_ms=([\d.]+)")
THRESHOLD_BEFORE_RE = re.compile(
    r"(?:under|below|above|over|than|exceed\w*|threshold|baseline(?:\s+of)?|<|>)"
    r"\s*(?:about|around|roughly|~)?\s*$",
    re.IGNORECASE,
)

LATENCY_TOLERANCE = 0.25  # fraction outside the observed min/max still accepted

@dataclass
class EvidenceReport:
    claims_checked: int = 0
    claims_supported: int = 0
    unsupported: list[str] = field(default_factory=list)
    cited_failure_ids: list[int] = field(default_factory=list)
    latency_supported: int = 0
    spacing_cited: bool = False

    @property
    def score(self) -> float | None:
        if self.claims_checked == 0:
            return None
        return round(self.claims_supported / self.claims_checked, 4)

    def to_dict(self) -> dict:
        return {
            "claims_checked": self.claims_checked,
            "claims_supported": self.claims_supported,
            "score": self.score,
            "unsupported": self.unsupported[:8],
            "cited_failure_ids": self.cited_failure_ids,
            "latency_supported": self.latency_supported,
            "spacing_cited": self.spacing_cited,
        }


def _expand_ids(group: str) -> list[int]:
    """Ids named by a citation. A range counts only its endpoints: models write
    "ids 1 through 29" to describe the span of a spread-out fault, which is not
    a claim that every id in between failed."""
    ids: list[int] = []
    for token in group.split(","):
        parts = [part for part in RANGE_SPLIT_RE.split(token.strip()) if part]
        if all(part.isdigit() for part in parts) and len(parts) in (1, 2):
            ids.extend(int(part) for part in parts)
    return ids


def _says_failure(before: str, after: str) -> bool:
    """Whether the sentence around a citation is actually talking about failures."""
    breaks = list(SENTENCE_BREAK_RE.finditer(before))
    clause_before = before[breaks[-1].end() :] if breaks else before
    end = SENTENCE_BREAK_RE.search(after)
    clause_after = after[: end.start() if end else len(after)][:FAILURE_LOOKAHEAD]
    return bool(FAILURE_WORD_RE.search(clause_before) or FAILURE_WORD_RE.search(clause_after))


def _cited_ids(explanation: str) -> dict[int, bool]:
    """Map each id claimed to have failed or succeeded to whether it succeeded.

    Models that can see the raw log often mention healthy requests in passing.
    A citation therefore counts only when its wording makes a claim, otherwise
    the check would penalise the raw-log condition for being specific.
    """
    cited: dict[int, bool] = {}
    for match in ID_MENTION_RE.finditer(explanation):
        before, after = explanation[: match.start()], explanation[match.end() :]
        if NEUTRAL_BEFORE_RE.search(before) or NEUTRAL_AFTER_RE.search(after):
            continue
        says_success = bool(SUCCESS_BEFORE_RE.search(before) or SUCCESS_AFTER_RE.search(after))
        if not says_success and not _says_failure(before, after):
            continue
        for request_id in _expand_ids(match.group(1)):
            cited.setdefault(request_id, says_success)
    return cited


def score_evidence(explanation: str, log_text: str, summary) -> EvidenceReport:
    """Check an explanation's factual claims against the log it describes."""
    report = EvidenceReport()
    if not explanation:
        return report
    report.spacing_cited = bool(SPACING_RE.search(explanation))

    summary_dict = summary.to_dict() if hasattr(summary, "to_dict") else dict(summary or {})
    error_ids = set(summary_dict.get("error_request_ids") or [])
    total_requests = int(summary_dict.get("total_requests") or 0)
    latency_mean = float(summary_dict.get("latency_ms_mean") or 0.0)
    observed = [float(value) for value in LOG_LATENCY_RE.findall(log_text)]

    for error_type in set(ERROR_TYPE_RE.findall(explanation)):
        report.claims_checked += 1
        if error_type in log_text:
            report.claims_supported += 1
        else:
            report.unsupported.append(f"error type '{error_type}' does not appear in the log")

    if total_requests:
        for request_id, says_success in sorted(_cited_ids(explanation).items()):
            report.claims_checked += 1
            if request_id >= total_requests:
                report.unsupported.append(
                    f"request id {request_id} is outside the run (only {total_requests} requests)"
                )
            elif says_success and request_id in error_ids:
                report.unsupported.append(f"request id {request_id} is described as healthy but failed")
            elif not says_success and request_id not in error_ids:
                report.unsupported.append(f"request id {request_id} did not fail in this run")
            else:
                report.claims_supported += 1
            if not says_success and request_id < total_requests:
                report.cited_failure_ids.append(request_id)

    if observed:
        floor = min(observed) * (1 - LATENCY_TOLERANCE)
        ceiling = max(observed) * (1 + LATENCY_TOLERANCE)
        seen: set[str] = set()
        for match in LATENCY_RE.finditer(explanation):
            raw = match.group(1)
            if raw in seen or THRESHOLD_BEFORE_RE.search(explanation[: match.start()]):
                continue
            seen.add(raw)
            value = float(raw)
            # The healthy-baseline figure comes from the prompt, not this log.
            if abs(value - 45.0) < 5.0 and abs(latency_mean - 45.0) > 50.0:
                continue
            report.claims_checked += 1
            if floor <= value <= ceiling or f"{raw}ms" in log_text:
                report.claims_supported += 1
                report.latency_supported += 1
            else:
                report.unsupported.append(
                    f"cited latency {value}ms is outside the observed {min(observed)}-{max(observed)}ms"
                )
        # Figures written as latency_ms=314.48 carry no "ms" unit. They do not
        # add a claim, but they do show the explanation used a measured latency.
        if not report.latency_supported and any(
            floor <= float(raw) <= ceiling for raw in LATENCY_FIELD_RE.findall(explanation)
        ):
            report.latency_supported = 1

    return report
