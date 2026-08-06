from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from typing import Any, cast

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
            "system",
            "voice",
        ),
    )
    parser.add_argument("text", nargs="?")
    parser.add_argument("apps_action", nargs="?")
    parser.add_argument("system_value", nargs="?")
    parser.add_argument("--display")
    parser.add_argument("--device")
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--vad-threshold", type=float)
    parser.add_argument("--energy-gate", type=float)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--name")
    parser.add_argument("--source")
    parser.add_argument("--kind")
    parser.add_argument("--all", action="store_true", dest="include_all")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--window-handle",
        type=int,
        help="target a specific visible application window (positive HWND)",
    )
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
        file_action = args.command == "voice" and args.text == "vad-file-test"
        if not file_action:
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
            if args.window_handle is not None and (
                args.window_handle <= 0
                or action not in {"focus", "minimize", "restore", "maximize", "close"}
            ):
                parser = argparse.ArgumentParser(prog="pangu apps")
                parser.error(
                    "--window-handle requires a positive value and a window control action"
                )
            before = {item.application_id for item in runtime.list_applications(True)}
            actions = {
                "discover": runtime.discover_applications,
                "refresh": runtime.refresh_applications,
                "list": lambda: runtime.list_applications(args.include_all),
                "resolve": lambda: runtime.resolve_application(name or ""),
                "status": lambda: runtime.application_status(name or ""),
                "windows": lambda: runtime.list_application_windows(name or ""),
                "open": lambda: runtime.open_application(name or ""),
                "focus": lambda: runtime.focus_application(name or "", args.window_handle),
                "minimize": lambda: runtime.minimize_application(name or "", args.window_handle),
                "maximize": lambda: runtime.maximize_application(name or "", args.window_handle),
                "restore": lambda: runtime.restore_application(name or "", args.window_handle),
                "close": lambda: runtime.close_application(
                    name or "", window_handle=args.window_handle
                ),
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
        elif args.command == "system":
            group, action, raw = args.text, args.apps_action, args.system_value
            if group == "volume":
                operations = {
                    None: "get_volume",
                    "set": "set_volume",
                    "increase": "increase_volume",
                    "decrease": "decrease_volume",
                }
                if action not in operations or (action and raw is None):
                    raise ValueError("invalid volume command")
                result = runtime.system_audio(
                    operations[action], int(raw) if raw is not None else None
                )
            elif group == "mute":
                operations = {
                    "status": "get_mute_state",
                    "on": "mute",
                    "off": "unmute",
                    "toggle": "toggle_mute",
                }
                if action not in operations or raw is not None:
                    raise ValueError("invalid mute command")
                result = runtime.system_audio(operations[action])
            elif group == "brightness":
                operations = {
                    None: "get_brightness",
                    "set": "set_brightness",
                    "increase": "increase_brightness",
                    "decrease": "decrease_brightness",
                }
                if action not in operations or (action and raw is None):
                    raise ValueError("invalid brightness command")
                result = runtime.system_brightness(
                    operations[action], int(raw) if raw is not None else None, args.display
                )
            else:
                raise ValueError("invalid system command")
            result = result.public()
            exit_code = _system_exit_code(result)
        elif args.command == "voice":
            action = args.text
            if action == "devices":
                discovery = runtime.voice.discover_devices(refresh=True)
                result = asdict(discovery)
                exit_code = _voice_exit_code(discovery.normalized_error)
            elif action == "diagnostics":
                result = runtime.voice.diagnostics().__dict__
                vad = cast(object, getattr(runtime.voice, "vad", None))
                if hasattr(vad, "diagnostics"):
                    result.update(cast(dict[str, object], cast(Any, vad).diagnostics()))
            elif action == "vad-model-status":
                vad = cast(object, getattr(runtime.voice, "vad", None))
                if not hasattr(vad, "diagnostics"):
                    raise ValueError("VAD model diagnostics unavailable")
                result = cast(dict[str, object], cast(Any, vad).diagnostics())
                exit_code = _voice_exit_code(str(result["vad_model_status"]))
            elif action == "capture-test":
                if not 1 <= args.seconds <= 30:
                    raise ValueError("--seconds must be between 1 and 30")
                from .voice import VoiceCaptureRequest

                capture = await runtime.voice.capture_test(
                    VoiceCaptureRequest(args.seconds, args.device)
                )
                result = asdict(capture)
                exit_code = _voice_exit_code(capture.normalized_error, capture.verification_state)
            elif action == "vad-file-test":
                if not args.apps_action:
                    raise ValueError("vad-file-test requires a WAV file")
                if args.vad_threshold is not None and not 0 < args.vad_threshold <= 1:
                    raise ValueError("--vad-threshold must be between 0 and 1")
                from pathlib import Path
                from .voice import (
                    BoundedWaveDecoder,
                    VadConfiguration,
                    VadFileInferenceResult,
                    VadFileInferenceService,
                )

                vad = cast(Any, getattr(runtime.voice, "vad", None))
                if (
                    vad is None
                    or not hasattr(vad, "initialize")
                    or vad.initialize().value != "AVAILABLE"
                ):
                    result = VadFileInferenceResult(
                        sanitized_input_name=Path(args.apps_action).name,
                        normalized_error="VAD_UNAVAILABLE",
                    )
                else:
                    config = VadConfiguration(
                        speech_threshold=args.vad_threshold or 0.5,
                        minimum_energy_floor=args.energy_gate or 0.01,
                    )
                    service = VadFileInferenceService(
                        BoundedWaveDecoder(),
                        vad,
                        lambda: __import__(
                            "pangu.voice", fromlist=["SpeechSegmentController"]
                        ).SpeechSegmentController(config, gate=args.energy_gate),
                        config,
                    )
                    result = service.infer(Path(args.apps_action))
                result = result.public()
                exit_code = _vad_file_exit_code(result)
            else:
                raise ValueError(
                    "voice supports devices, diagnostics, capture-test, or vad-model-status"
                )
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


def _system_exit_code(value: dict[str, object]) -> int:
    error = value.get("normalized_error")
    return {
        "NOT_FOUND": 3,
        "AMBIGUOUS": 4,
        "DENIED": 5,
        "NATIVE_FAILURE": 6,
        "UNSUPPORTED": 7,
        "AUDIO_ADAPTER_UNAVAILABLE": 7,
        "NO_ACTIVE_AUDIO_ENDPOINT": 7,
        "BRIGHTNESS_ADAPTER_UNAVAILABLE": 7,
        "NO_COMPATIBLE_DISPLAY": 7,
        "POSTCONDITION_TIMEOUT": 8,
    }.get(str(error), 0)


def _voice_exit_code(error: str | None, state: str = "VERIFIED") -> int:
    if error in {"NO_INPUT_DEVICE", "STALE_DEVICE_SELECTOR", "MISSING", "INVALID_CHECKSUM"}:
        return 3
    if error == "AMBIGUOUS_DEVICE":
        return 4
    if error in {"PORTAUDIO_FAILURE", "STREAM_FAILURE", "DEVICE_DISCONNECTED"}:
        return 6
    if error == "BACKEND_UNAVAILABLE":
        return 7
    if error in {"LOAD_FAILED", "CLOSED"}:
        return 6
    return 0 if state == "VERIFIED" else 8


def _vad_file_exit_code(result: dict[str, object]) -> int:
    error = str(result.get("normalized_error") or "")
    if result.get("verification_state") == "VERIFIED":
        return 0
    if error in {"WAV_FILE_NOT_FOUND", "VAD_UNAVAILABLE"}:
        return 3
    if error in {"WAV_UNSUPPORTED"}:
        return 7
    if error in {
        "WAV_PATH_INVALID",
        "WAV_FORMAT_INVALID",
        "WAV_TRUNCATED",
        "WAV_DECODE_FAILED",
        "VAD_INFERENCE_FAILED",
    }:
        return 6
    return 8


if __name__ == "__main__":
    raise SystemExit(main())
