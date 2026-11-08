"""The provider-neutral seam.

Every condition in the experiment makes exactly one `complete()` call. Keeping
that call behind a narrow interface is what lets the same pipeline run against
Claude and against Groq-hosted open models without either path getting special
treatment -- which is the precondition for the cross-model comparison meaning
anything.

Deliberate design decision: both providers are driven by *plain prompting plus
tolerant JSON parsing*, not by provider-specific structured-output machinery
(Anthropic's `output_config.format`, OpenAI's `response_format`). Those
mechanisms differ in strength, so using each provider's best one would make
"malformed output rate" a measurement of the enforcement layer rather than of
the model. Symmetry costs a little reliability and buys a valid comparison.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class LLMResponse:
    """One model call, with everything needed to score and cost it afterwards."""

    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: str | None = None
    raw_meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


@runtime_checkable
class LLMClient(Protocol):
    """The entire provider contract. Four lines, on purpose."""

    provider: str
    model: str

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResponse:
        ...


class ProviderError(RuntimeError):
    """Raised when a provider cannot be constructed (missing key, bad model)."""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> tuple[dict | None, str | None]:
    """Tolerantly pull a JSON object out of a model response.

    Returns (parsed, error). Smaller open models wrap JSON in prose or code
    fences far more often than Claude does, so this runs identically for every
    provider and the failure is *recorded* rather than retried away -- the
    malformed rate is itself one of the experiment's metrics.
    """
    if not text or not text.strip():
        return None, "empty response"

    candidates: list[str] = []

    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1))

    candidates.append(text.strip())

    # Last resort: the outermost {...} span in the response.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed, None

    return None, "no parseable JSON object in response"
