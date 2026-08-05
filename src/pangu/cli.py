from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runtime_builder import RuntimeBuilder


def main() -> None:
    parser = argparse.ArgumentParser(prog="pangu")
    parser.add_argument(
        "command",
        choices=("health", "models", "model-health", "normalize", "sanitize", "route", "decide"),
    )
    parser.add_argument("text", nargs="?")
    args = parser.parse_args()
    container = RuntimeBuilder(Path.cwd()).build()
    runtime = container.runtime
    runtime.start()
    try:
        text = args.text or ""
        if args.command == "health":
            result: object = runtime.db.health_details()
        elif args.command == "models":
            result = {
                "capabilities": [item.__dict__ for item in container.model_capabilities.all()]
            }
        elif args.command == "model-health":
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


if __name__ == "__main__":
    main()
