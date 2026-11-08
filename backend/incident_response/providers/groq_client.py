"""Groq-hosted open models, via the `openai` SDK pointed at Groq's endpoint.

Groq's free tier is what makes the capability axis affordable: the same 1-call
pipeline can be run against models weak enough to actually fail the task, which
is where the difference between raw-log input and structured-summary input
should show up. Opus solves the task either way, so on Claude alone the
experiment has no headroom.

Model ids on Groq rotate as models are added and retired, so the catalogue here
is a fallback -- `list_live_models()` asks the API what is actually served.
"""

from __future__ import annotations

import os
import time

from .base import LLMResponse, ProviderError

PROVIDER = "groq"
BASE_URL = "https://api.groq.com/openai/v1"

DEFAULT_MODEL = "openai/gpt-oss-120b"

# Fallback catalogue, ordered roughly strongest-to-weakest. Override with
# GROQ_MODELS="a,b,c" or just read the live list from the API.
FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
]


def configured_models() -> list[str]:
    override = os.environ.get("GROQ_MODELS", "").strip()
    if override:
        return [m.strip() for m in override.split(",") if m.strip()]
    return list(FALLBACK_MODELS)


def list_live_models(api_key: str | None = None) -> list[str]:
    """Ask Groq what it currently serves. Falls back to the static catalogue."""
    key = api_key or os.environ.get("GROQ_API_KEY")
    if not key:
        return configured_models()
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, base_url=BASE_URL)
        models = client.models.list()
        ids = sorted(m.id for m in models.data)
        # Filter out non-chat endpoints (whisper, guard, tts) -- they cannot
        # serve this task and would only clutter the picker.
        skip = ("whisper", "tts", "orpheus", "guard", "embed", "distil")
        return [m for m in ids if not any(s in m.lower() for s in skip)] or configured_models()
    except Exception:  # noqa: BLE001 - a dead model list must not break the app
        return configured_models()


def verify_key(key: str) -> None:
    """Raise if the key is not accepted. Listing models spends no tokens."""
    from openai import OpenAI

    OpenAI(api_key=key, base_url=BASE_URL, timeout=10.0, max_retries=0).models.list()


class GroqClient:
    provider = PROVIDER

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("the `openai` package is not installed") from exc

        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise ProviderError("GROQ_API_KEY is not set")

        self.model = model
        self._client = OpenAI(api_key=key, base_url=BASE_URL)

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResponse:
        started = time.perf_counter()
        try:
            completion = self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0,  # determinism; accepted on the OpenAI-compatible path
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as data
            return LLMResponse(
                text="",
                provider=self.provider,
                model=self.model,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
            )

        elapsed = (time.perf_counter() - started) * 1000
        choice = completion.choices[0] if completion.choices else None
        text = (choice.message.content or "") if choice else ""

        usage = getattr(completion, "usage", None)
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=elapsed,
            raw_meta={"finish_reason": getattr(choice, "finish_reason", None) if choice else None},
        )
