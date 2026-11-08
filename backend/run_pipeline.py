#!/usr/bin/env python
"""Multi-agent pipeline: Log Correlator -> Hypothesis Agent (LLM) -> Runbook Agent.

Still exactly one LLM call -- the first and last steps are deterministic.

    python run_pipeline.py --log logs/pool_exhausted_run1.log
    python run_pipeline.py --log logs/pool_exhausted_run1.log --provider groq --trace
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from incident_response.conditions import diagnose_stream
from incident_response.providers import PROVIDERS, get_client
from incident_response.providers.base import ProviderError


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--provider", default="anthropic", choices=sorted(PROVIDERS))
    parser.add_argument("--model", default=None)
    parser.add_argument("--recovery", action="store_true", help="also ask for a recovery action")
    parser.add_argument("--trace", action="store_true", help="print each agent step as it runs")
    args = parser.parse_args()

    if not args.log.exists():
        sys.exit(f"log not found: {args.log}")

    try:
        client = get_client(args.provider, args.model)
    except ProviderError as exc:
        sys.exit(f"provider error: {exc}")

    log_text = args.log.read_text(encoding="utf-8")
    final = None

    for event in diagnose_stream(log_text, client, "pipeline", recovery=args.recovery):
        if event["type"] == "result":
            final = event["result"]
        elif args.trace and event["type"] == "step_done":
            print(f"--- {event['agent']} ({event['kind']}) ---")
            print(json.dumps(event["output"], indent=2))
            print()

    if final is None:
        sys.exit("pipeline produced no result")

    print(
        json.dumps(
            {
                "log_summary": final["log_summary"],
                "predicted_fault": final["predicted_fault"],
                "explanation": final["explanation"],
                "confidence": final["confidence"],
                "ranked_alternatives": final["ranked_alternatives"],
                "remediation": final["remediation"],
                "evidence": final["evidence"],
                "recovery": final["recovery"],
                "agent_call_log": final["agent_call_log"],
                "llm_call_count": final["llm_call_count"],
                "provider": final["provider"],
                "model": final["model"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
