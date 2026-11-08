"""Provider registry: one place that knows which backends exist and how to build one."""

from __future__ import annotations

import os

from .anthropic_client import MODELS as ANTHROPIC_MODELS
from .anthropic_client import DEFAULT_MODEL as ANTHROPIC_DEFAULT
from .anthropic_client import AnthropicClient
from .anthropic_client import verify_key as verify_anthropic_key
from .base import LLMClient, LLMResponse, ProviderError, extract_json
from .groq_client import DEFAULT_MODEL as GROQ_DEFAULT
from .groq_client import GroqClient, configured_models, list_live_models
from .groq_client import verify_key as verify_groq_key
from .mock_client import MODELS as MOCK_MODELS
from .mock_client import DEFAULT_MODEL as MOCK_DEFAULT
from .mock_client import MockClient

__all__ = [
    "LLMClient",
    "LLMResponse",
    "ProviderError",
    "extract_json",
    "get_client",
    "provider_catalog",
    "verify_key",
    "PROVIDERS",
]

PROVIDERS = {
    "anthropic": {
        "label": "Anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "factory": AnthropicClient,
        "verify": verify_anthropic_key,
        "default_model": ANTHROPIC_DEFAULT,
        "models": ANTHROPIC_MODELS,
        "comparable": True,
        "note": "Paid. Current Opus; select claude-opus-5 for parity with the Phase 0 proposal.",
    },
    "groq": {
        "label": "Groq",
        "env_key": "GROQ_API_KEY",
        "factory": GroqClient,
        "verify": verify_groq_key,
        "default_model": GROQ_DEFAULT,
        "models": None,  # resolved live; see provider_catalog()
        "comparable": True,
        "note": "Free tier. Open models across a capability range -- the second axis.",
    },
    "mock": {
        "label": "Mock (offline)",
        "env_key": None,
        "factory": MockClient,
        "default_model": MOCK_DEFAULT,
        "models": MOCK_MODELS,
        "comparable": False,
        "note": "Deterministic rule engine for offline development. Not a valid condition.",
    },
}


def has_key(provider: str) -> bool:
    spec = PROVIDERS.get(provider)
    if not spec:
        return False
    env_key = spec["env_key"]
    if env_key is None:
        return True
    return bool(os.environ.get(env_key))


def models_for(provider: str, live: bool = False) -> list[str]:
    spec = PROVIDERS.get(provider)
    if not spec:
        return []
    if provider == "groq":
        return list_live_models() if live and has_key("groq") else configured_models()
    return list(spec["models"] or [])


def provider_catalog(live: bool = False) -> list[dict]:
    """Everything the UI needs to render the provider/model pickers."""
    catalog = []
    for name, spec in PROVIDERS.items():
        catalog.append(
            {
                "provider": name,
                "label": spec["label"],
                "available": has_key(name),
                "env_key": spec["env_key"],
                "default_model": spec["default_model"],
                "models": models_for(name, live=live),
                "comparable": spec["comparable"],
                "note": spec["note"],
            }
        )
    return catalog


def verify_key(provider: str, value: str) -> tuple[bool, str]:
    """Check a key with the provider. Exception text is dropped: it can echo the key."""
    try:
        PROVIDERS[provider]["verify"](value)
    except Exception as exc:  # noqa: BLE001 - reported to the user as a result
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return False, "The provider rejected this key."
        detail = f"HTTP {status}" if status else type(exc).__name__
        return False, f"Could not verify this key ({detail})."
    return True, "Key accepted."


def get_client(provider: str, model: str | None = None) -> LLMClient:
    spec = PROVIDERS.get(provider)
    if not spec:
        raise ProviderError(f"unknown provider '{provider}'")
    return spec["factory"](model=model or spec["default_model"])
