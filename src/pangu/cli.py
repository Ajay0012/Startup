from __future__ import annotations

import argparse
import asyncio
import json
from typing import cast

from .applications import (
    ApplicationOperationResult,
    ApplicationRecord,
    ApplicationWindowsResult,
    ResolutionResult,
    ResolutionStatus,
    VerificationState,
)
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
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--name")
    parser.add_argument("--source")
    parser.add_argument("--kind")
    parser.add_argument("--all", action="store_true", dest="include_all")
    parser.add_argument("--json", action="store_true", dest="as_json")
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
            before = {item.application_id for item in runtime.list_applications(True)}
            actions = {
                "discover": runtime.discover_applications,
                "refresh": runtime.refresh_applications,
                "list": lambda: runtime.list_applications(args.include_all),
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
            if action == "list":
                records = cast(list[ApplicationRecord], value)
                if args.name:
                    records = [
                        x for x in records if args.name.casefold() in x.display_name.casefold()
                    ]
                if args.source:
                    records = [
                        x for x in records if x.install_source.casefold() == args.source.casefold()
                    ]
                if args.kind:
                    records = [
                        x for x in records if x.application_kind.casefold() == args.kind.casefold()
                    ]
                value = records[: max(args.limit, 0)]
            result = (
                [item.public() if hasattr(item, "public") else item.__dict__ for item in value]
                if isinstance(value, list)
                else value.public()
                if hasattr(value, "public")
                else value.__dict__
            )
            if action in {"discover", "refresh"} and not (args.as_json or args.include_all):
                records = cast(list[ApplicationRecord], value)
                user_count = sum(x.application_kind == "USER_APPLICATION" for x in records)
                result = {
                    "total_discovered": len(records),
                    "user_applications": user_count,
                    "excluded_system_records": len(records) - user_count,
                    "new_records": len([x for x in records if x.application_id not in before]),
                    "updated_records": len([x for x in records if x.application_id in before]),
                    "stale_records": sum(x.stale for x in records),
                    "discovery_run_status": "completed",
                }
            exit_code = _application_exit_code(value)
        else:
            result = runtime.decide(text).__dict__
        if args.command == "apps" and action == "list" and not args.as_json:
            records = cast(list[ApplicationRecord], value)
            print(
                "application_id  display_name  kind  source  confidence  running  launch_eligible"
            )
            running = {x.name.casefold() for x in runtime.application_control.adapter.processes()}
            for item in records:
                print(
                    f"{item.application_id[:12]}  {item.display_name}  {item.application_kind}  "
                    f"{item.install_source}  {item.confidence:.2f}  "
                    f"{(item.executable_name or '').casefold() in running}  {item.launch_eligible}"
                )
        else:
            print(json.dumps(result, default=str))
        return exit_code
    finally:
        try:
            await container.gemini_provider.close()
        finally:
            await runtime.stop_async()


def main() -> int:
    return asyncio.run(run_command(parse_args()))


def _application_exit_code(value: object) -> int:
    if isinstance(value, (ResolutionResult, ApplicationWindowsResult)):
        return {
            ResolutionStatus.NOT_FOUND: 3,
            ResolutionStatus.AMBIGUOUS: 4,
            ResolutionStatus.UNSUPPORTED: 7,
        }.get(value.status, 0)
    if isinstance(value, ApplicationOperationResult):
        if value.normalized_error == ResolutionStatus.NOT_FOUND:
            return 3
        if value.normalized_error == ResolutionStatus.AMBIGUOUS:
            return 4
        return {
            VerificationState.DENIED: 5,
            VerificationState.FAILED: 6,
            VerificationState.UNSUPPORTED: 7,
            VerificationState.UNVERIFIED: 8,
            VerificationState.PARTIALLY_VERIFIED: 8,
        }.get(value.verification_state, 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
