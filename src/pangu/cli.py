from __future__ import annotations

import argparse
import asyncio
import json

from .runtime_builder import RuntimeBuilder
from .settings import resolve_application_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pangu")
    parser.add_argument(
        "command",
        choices=(
            "health",
            "models",
            "model-health",
            "normalize",
            "sanitize",
            "route",
            "decide",
            "apps",
        ),
    )
    parser.add_argument("text", nargs="?")
    parser.add_argument("apps_action", nargs="?")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="perform one context-free live Gemini health request",
    )
    args = parser.parse_args()
    if args.probe and args.command != "model-health":
        parser.error("--probe is only supported with model-health")
    return args


async def run_command(args: argparse.Namespace) -> int:
    """Run the complete CLI lifecycle on one event loop."""
    container = RuntimeBuilder(resolve_application_root()).build()
    runtime = container.runtime
    try:
        await runtime.start_async()
        text = args.text or ""
        exit_code = 0
        if args.command == "health":
            result: object = runtime.db.health_details()
        elif args.command == "models":
            result = {
                "capabilities": [item.__dict__ for item in container.model_capabilities.all()]
            }
        elif args.command == "model-health":
            if args.probe:
                probe = await container.gemini_provider.probe_async(
                    container.settings.gemini_timeout_seconds
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
        elif args.command == "apps":
            action = args.text or "list"
            name = args.apps_action
            actions = {
                "discover": runtime.discover_applications,
                "refresh": runtime.refresh_applications,
                "list": runtime.list_applications,
                "resolve": lambda: runtime.resolve_application(name or ""),
                "status": lambda: runtime.application_status(name or ""),
                "windows": lambda: runtime.list_application_windows(name or ""),
                "open": lambda: runtime.open_application(name or ""),
                "focus": lambda: runtime.focus_application(name or ""),
                "minimize": lambda: runtime.minimize_application(name or ""),
                "maximize": lambda: runtime.maximize_application(name or ""),
                "restore": lambda: runtime.restore_application(name or ""),
                "close": lambda: runtime.close_application(name or ""),
                "restart": lambda: runtime.restart_application(name or ""),
            }
            value = actions[action]()  # type: ignore[no-untyped-call]
            result = (
                [item.public() if hasattr(item, "public") else item.__dict__ for item in value]
                if isinstance(value, list)
                else value.__dict__
            )
        else:
            result = runtime.decide(text).__dict__
        print(json.dumps(result, default=str))
        return exit_code
    finally:
        try:
            await container.gemini_provider.close()
        finally:
            await runtime.stop_async()


def main() -> int:
    return asyncio.run(run_command(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
