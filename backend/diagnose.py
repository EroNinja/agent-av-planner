#!/usr/bin/env python
"""Single-agent baseline: one LLM call, raw log text in, classification out.

    python diagnose.py --log logs/pool_exhausted_run1.log
    python diagnose.py --log logs/pool_exhausted_run1.log --provider groq
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from incident_response.conditions import diagnose
from incident_response.providers import PROVIDERS, get_client
from incident_response.providers.base import ProviderError


def add_provider_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", default="anthropic", choices=sorted(PROVIDERS))
    parser.add_argument("--model", default=None, help="defaults to the provider's default model")
    parser.add_argument("--recovery", action="store_true", help="also ask for a recovery action")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, required=True)
    add_provider_args(parser)
    args = parser.parse_args()

    if not args.log.exists():
        sys.exit(f"log not found: {args.log}")

    try:
        client = get_client(args.provider, args.model)
    except ProviderError as exc:
        sys.exit(f"provider error: {exc}")

    result = diagnose(args.log.read_text(encoding="utf-8"), client, "single_agent", recovery=args.recovery)

    print(
        json.dumps(
            {
                "predicted_fault": result.predicted_fault,
                "explanation": result.explanation,
                "confidence": result.confidence,
                "remediation": result.remediation,
                "evidence": result.evidence,
                "recovery": result.recovery,
                "provider": result.provider,
                "model": result.model,
                "llm_call_count": result.llm_call_count,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
