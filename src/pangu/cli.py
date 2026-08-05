from __future__ import annotations

import argparse
import asyncio
import json
from .runtime_builder import RuntimeBuilder
from .settings import resolve_application_root


def main() -> int:
    parser = argparse.ArgumentParser(prog="pangu")
    parser.add_argument(
        "command",
        choices=("health", "models", "model-health", "normalize", "sanitize", "route", "decide"),
    )
    parser.add_argument("text", nargs="?")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="perform one context-free live Gemini health request",
    )
    args = parser.parse_args()
    if args.probe and args.command != "model-health":
        parser.error("--probe is only supported with model-health")
    container = RuntimeBuilder(resolve_application_root()).build()
    runtime = container.runtime
    runtime.start()
    exit_code = 0
    try:
        text = args.text or ""
        if args.command == "health":
            result: object = runtime.db.health_details()
        elif args.command == "models":
            result = {
                "capabilities": [item.__dict__ for item in container.model_capabilities.all()]
            }
        elif args.command == "model-health":
            if args.probe:
                probe = asyncio.run(
                    container.gemini_provider.probe_async(container.settings.gemini_timeout_seconds)
                )
                exit_code = 0 if probe.health.value == "HEALTHY" else 1
            result = {
                "deterministic": container.deterministic_provider.health(),
                "gemini": container.gemini_provider.health_details(),
            }
        elif args.command == "normalize":
            result = runtime.language.normalize(text).__dict__
        elif args.command == "sanitize":
            decision = container.sanitizer.sanitize(text)
            result = {
                "outcome": decision.outcome,
                "sanitized_content": decision.sanitized_content,
                "redactions": decision.redactions,
            }
        elif args.command == "route":
            intent = runtime.language.normalize(text)
            result = container.model_router.route(
                intent.canonical_english, intent.intent_name != "informational"
            ).__dict__
        else:
            result = runtime.decide(text).__dict__
        print(json.dumps(result, default=str))
    finally:
        runtime.stop()
        if args.probe:
            asyncio.run(container.gemini_provider.close())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
