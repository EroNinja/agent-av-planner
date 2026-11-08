"""Claude, via the official `anthropic` SDK.

Not an OpenAI-compatible shim: routing Claude through a compatibility layer
would silently drop effort control and typed errors, and we would end up
measuring the shim.
"""

from __future__ import annotations

import os
import time

from .base import LLMResponse, ProviderError, ToolCall, ToolTurn

PROVIDER = "anthropic"

# `claude-opus-5-5` is the current Opus and the default here. The original
# proposal pins `claude-opus-5`; select it explicitly for byte-for-byte parity
# with the Phase 0 write-up.
DEFAULT_MODEL = "claude-opus-5-5"

MODELS = [
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
]

# Effort is pinned low and *identically across both conditions*. On current
# Claude models thinking is always on and bills as output, so leaving effort at
# its default would let the raw-log condition quietly spend more reasoning than
# the structured-summary condition -- the two arms would no longer be matched on
# compute, only on call count. Pinning it restores the control.
EFFORT = "low"


def verify_key(key: str) -> None:
    """Raise if the key is not accepted. Listing models spends no tokens."""
    from anthropic import Anthropic

    Anthropic(api_key=key, timeout=10.0, max_retries=0).models.list()


class AnthropicClient:
    provider = PROVIDER

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None):
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("the `anthropic` package is not installed") from exc

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")

        self.model = model
        self._client = Anthropic(api_key=key)

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResponse:
        started = time.perf_counter()
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": EFFORT},
        )

        try:
            message = self._client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as data
            return LLMResponse(
                text="",
                provider=self.provider,
                model=self.model,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
            )

        elapsed = (time.perf_counter() - started) * 1000

        if getattr(message, "stop_reason", None) == "refusal":
            return LLMResponse(
                text="",
                provider=self.provider,
                model=self.model,
                latency_ms=elapsed,
                error="refusal",
            )

        # Thinking blocks come back alongside text blocks and are empty by
        # default -- take only the text.
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )

        usage = getattr(message, "usage", None)
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=elapsed,
            raw_meta={"stop_reason": getattr(message, "stop_reason", None), "effort": EFFORT},
        )

    def complete_with_tools(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048) -> ToolTurn:
        started = time.perf_counter()
        chat: list[dict] = []
        for message in messages:
            if message["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"]}
                if chat and chat[-1]["role"] == "user" and isinstance(chat[-1]["content"], list):
                    chat[-1]["content"].append(block)
                else:
                    chat.append({"role": "user", "content": [block]})
            elif message["role"] == "assistant":
                # Thinking and tool_use blocks must be replayed exactly as received.
                chat.append({"role": "assistant", "content": message.get("provider_state") or message.get("content", "")})
            else:
                chat.append({"role": "user", "content": message["content"]})

        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": chat,
            "output_config": {"effort": EFFORT},
        }
        if tools:
            kwargs["tools"] = [
                {"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}
                for tool in tools
            ]

        try:
            response = self._client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as data
            return ToolTurn(text="", latency_ms=(time.perf_counter() - started) * 1000, error=f"{type(exc).__name__}: {exc}")

        text = "".join(block.text for block in response.content if block.type == "text")
        calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
            for block in response.content
            if block.type == "tool_use"
        ]
        return ToolTurn(
            text=text,
            tool_calls=calls,
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            latency_ms=(time.perf_counter() - started) * 1000,
            error="refusal" if response.stop_reason == "refusal" else None,
            provider_state=[block.model_dump(exclude_none=True) for block in response.content],
        )
