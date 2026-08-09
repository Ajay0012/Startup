from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .applications import (
    ApplicationControlRuntime,
    ApplicationOperationResult,
    ApplicationRecord,
    ApplicationWindowsResult,
    ResolutionResult,
    ResolutionStatus,
    VerificationState,
)
from .capabilities import CapabilityCatalog
from .contracts import CommandEnvelope, Status, ToolRequest, ToolResult
from .database import DatabaseService
from .events import EventBus, EventEnvelope
from .language import LanguageRuntime
from .lifecycle import LifecycleKernel, LifecycleService
from .memory import MemoryKind, PersistentMemoryRuntime
from .missions import PersistentMissionRuntime
from .model_runtime import (
    CognitiveDecision,
    CognitiveEngine,
    ContextAssembler,
    ModelRequest,
    ModelRole,
    ModelRouter,
)
from .permissions import PermissionGrant, PermissionStore
from .security import ApprovalStore, SafetyGateway
from .system_control import SystemControlResult, SystemControlRuntime, SystemVerification
from .tools import ToolRuntime
from .voice import VoiceSessionRuntime
from .world_model import PersonalWorldModel, WorldDelta


class Runtime:
    """Runtime shell; every shared service is supplied by the composition root."""

    def __init__(
        self,
        root: Path,
        settings: object,
        database: DatabaseService,
        lifecycle: LifecycleKernel,
        events: EventBus,
        catalog: CapabilityCatalog,
        language: LanguageRuntime,
        context: ContextAssembler,
        model_router: ModelRouter,
        cognitive_engine: CognitiveEngine,
        application_control: ApplicationControlRuntime,
        system_control: SystemControlRuntime,
        voice: VoiceSessionRuntime,
        memory: PersistentMemoryRuntime | None = None,
        world_model: PersonalWorldModel | None = None,
        missions: PersistentMissionRuntime | None = None,
    ) -> None:
        self.root, self.settings = root, settings
        self.db, self.lifecycle, self.events, self.catalog = database, lifecycle, events, catalog
        self.language, self.context, self.model_router, self.cognitive_engine = (
            language,
            context,
            model_router,
            cognitive_engine,
        )
        self.safety = SafetyGateway()
        self.application_control = application_control
        self.system_control = system_control
        self.voice = voice
        self.memory = memory
        self.world_model = world_model
        self.missions = missions
        grants = PermissionStore((PermissionGrant("filesystem.write:*", "default"),))
        # Legacy generic tools currently expose only read/low-risk operations. High-risk
        # Windows controls use their persistent exact-approval runtimes instead.
        self.approvals = ApprovalStore()
        self.tools = ToolRuntime(root, self.safety, self.catalog, grants, self.approvals)
        self.started = False
        self.last_context: dict[str, object] = {}

        async def start_database() -> None:
            self.db.start()

        async def stop_database() -> None:
            self.db.stop()

        self.lifecycle.register(LifecycleService("database", start_database, stop_database))
        self.lifecycle.register(LifecycleService("events", self.events.start, self.events.stop))
        self.lifecycle.register(
            LifecycleService("voice", self.voice.start, self.voice.stop, ("events",))
        )

    def start(self) -> None:
        asyncio.run(self.start_async())

    async def start_async(self) -> None:
        await self.lifecycle.start()
        self.application_control.catalog.load()
        self.started = self.db.health_details()["database_ready"] is True

    def stop(self) -> None:
        asyncio.run(self.stop_async())

    async def stop_async(self) -> None:
        await self.lifecycle.stop()
        self.started = False

    def _grounding(self, text: str) -> tuple[str, ...]:
        grounded: list[str] = []
        if self.memory is not None:
            try:
                for item in self.memory.recall(text, limit=5):
                    grounded.append(
                        f"memory[{item.kind.value}] {item.subject}: {json.dumps(item.content, ensure_ascii=False, default=str)}"
                    )
            except RuntimeError:
                pass
        if self.world_model is not None:
            try:
                for fact in self.world_model.snapshot(limit=12):
                    grounded.append(
                        f"world {fact.entity}.{fact.attribute}={json.dumps(fact.value, ensure_ascii=False, default=str)}"
                    )
            except RuntimeError:
                pass
        return tuple(grounded[-12:])

    def decide(self, text: str) -> CognitiveDecision:
        intent = self.language.normalize(text)
        self.last_context = self.context.assemble(intent.canonical_english, self._grounding(text))
        deterministic = intent.intent_name in {
            "create_folder",
            "battery_status",
            "open_application",
            "focus_application",
            "minimize_application",
            "maximize_application",
            "restore_application",
            "close_application",
            "restart_application",
            "get_volume",
            "set_volume",
            "increase_volume",
            "decrease_volume",
            "mute",
            "unmute",
            "toggle_mute",
            "get_mute_state",
            "mute_volume",
            "volume_down",
            "get_brightness",
            "set_brightness",
            "increase_brightness",
            "decrease_brightness",
            "remember",
            "recall_memory",
        }
        route = self.model_router.route(intent.canonical_english, deterministic)
        return self.cognitive_engine.decide(intent.intent_name, route, intent.original_text)

    @staticmethod
    def _system_result(request_id: str, result: SystemControlResult) -> ToolResult:
        if result.verification_state == SystemVerification.VERIFIED:
            status = Status.VERIFIED
        elif result.verification_state == SystemVerification.DENIED:
            status = Status.DENIED
        elif result.verification_state == SystemVerification.FAILED:
            status = Status.FAILED
        else:
            status = Status.UNVERIFIED
        if result.observed_value is not None:
            message = f"{result.operation.replace('_', ' ')}: {result.observed_value}"
        elif result.evidence.get("displays"):
            message = "Display brightness information is available."
        else:
            message = result.normalized_error.value if result.normalized_error else result.observed_outcome
        return ToolResult(request_id, status, message, result.public(), dict(result.evidence))

    @staticmethod
    def _application_result(request_id: str, result: ApplicationOperationResult) -> ToolResult:
        if result.verification_state == VerificationState.VERIFIED:
            status = Status.VERIFIED
        elif result.verification_state == VerificationState.DENIED:
            status = Status.DENIED
        elif result.verification_state == VerificationState.FAILED:
            status = Status.FAILED
        else:
            status = Status.UNVERIFIED
        message = (
            f"{result.requested_target}: {result.observed_outcome}"
            if not result.normalized_error
            else f"{result.requested_target}: {result.normalized_error}"
        )
        return ToolResult(request_id, status, message, dict(result.evidence), {"confidence": result.confidence})

    def _remember_turn(self, command: CommandEnvelope, intent_name: str, result: ToolResult) -> None:
        if self.memory is None:
            return
        self.memory.remember(
            MemoryKind.EPISODIC,
            f"turn {command.command_id}",
            {
                "utterance": command.original_utterance,
                "source": command.source,
                "intent": intent_name,
                "status": result.status.value,
                "result": result.message,
            },
            importance=0.45 if result.status == Status.VERIFIED else 0.35,
            confidence=1.0,
            source=command.source,
        )

    def _informational(self, command: CommandEnvelope, text: str) -> ToolResult:
        grounding = self._grounding(text)
        context = self.context.assemble(text, grounding)
        route = self.model_router.route(text, deterministic_available=False)
        role = route.role or ModelRole.FAST
        prompt = (
            "You are PANGU, a concise personal Windows assistant. Answer the owner's question naturally. "
            "Use supplied local context only when relevant. Never claim a computer action happened unless a "
            "tool result verified it. Do not expose secrets.\n\n"
            f"Owner: {text}\n"
            f"Local context: {json.dumps(context, ensure_ascii=False, default=str)}"
        )
        try:
            result = asyncio.run(
                self.model_router.gemini.generate_async(
                    ModelRequest(prompt, role=role, trace_id=command.trace_id, mission_id="conversation")
                )
            )
        except RuntimeError:
            return ToolResult(
                command.command_id,
                Status.UNVERIFIED,
                "Conversational reasoning is unavailable in the current execution context.",
            )
        if result.text:
            return ToolResult(
                command.command_id,
                Status.EXECUTED,
                result.text.strip(),
                {"provider": result.provider, "model": result.model},
                {"health": result.health.value},
            )
        return ToolResult(
            command.command_id,
            Status.UNVERIFIED,
            "Gemini reasoning is unavailable right now.",
            {"provider": result.provider, "model": result.model},
            {"health": result.health.value, "error": str(result.error) if result.error else None},
        )

    def command(self, text: str, source: str = "cli") -> ToolResult:
        if not self.started:
            raise RuntimeError("runtime not started")
        command = CommandEnvelope(text, source)
        intent = self.language.normalize(text)
        record_original = True

        if intent.intent_name == "create_folder":
            request = ToolRequest(
                "filesystem", "create_folder", {"name": intent.entities.get("name", "New Folder")}
            )
            result = self.tools.execute(request)
        elif intent.intent_name == "battery_status":
            result = self.tools.execute(ToolRequest("system", "battery_status", {}))
        elif intent.intent_name in {
            "open_application",
            "focus_application",
            "minimize_application",
            "maximize_application",
            "restore_application",
            "close_application",
            "restart_application",
        }:
            operation = intent.intent_name.removesuffix("_application")
            app_name = intent.entities.get("application", "").strip()
            app_result = self.application_control.operate(operation, app_name)
            result = self._application_result(command.command_id, app_result)
        elif intent.intent_name in {
            "get_volume",
            "set_volume",
            "increase_volume",
            "decrease_volume",
            "mute",
            "unmute",
            "toggle_mute",
            "get_mute_state",
            "mute_volume",
            "volume_down",
        }:
            operation = {"mute_volume": "mute", "volume_down": "decrease_volume"}.get(
                intent.intent_name, intent.intent_name
            )
            raw_value = intent.entities.get("value", intent.entities.get("step"))
            value = int(raw_value) if raw_value is not None else None
            system_result = self.system_control.audio(operation, value)
            result = self._system_result(command.command_id, system_result)
            record_original = False  # SystemControlRuntime already emits its verified audit record.
        elif intent.intent_name in {
            "get_brightness",
            "set_brightness",
            "increase_brightness",
            "decrease_brightness",
        }:
            raw_value = intent.entities.get("value", intent.entities.get("step"))
            value = int(raw_value) if raw_value is not None else None
            system_result = self.system_control.brightness(intent.intent_name, value)
            result = self._system_result(command.command_id, system_result)
            record_original = False
        elif intent.intent_name == "remember" and self.memory is not None:
            text_to_remember = intent.entities.get("memory", "").strip()
            memory = self.memory.remember(
                MemoryKind.SEMANTIC,
                text_to_remember[:160],
                {"text": text_to_remember},
                importance=0.8,
                confidence=1.0,
                source="owner",
            )
            result = ToolResult(
                command.command_id,
                Status.VERIFIED,
                "I'll remember that.",
                {"memory_id": memory.memory_id, "kind": memory.kind.value},
            )
        elif intent.intent_name == "recall_memory" and self.memory is not None:
            matches = self.memory.recall(intent.entities.get("query", ""), limit=5)
            message = (
                "I remember: "
                + "; ".join(str(item.content.get("text", item.subject)) for item in matches)
                if matches
                else "I don't have a matching stored memory."
            )
            result = ToolResult(
                command.command_id,
                Status.VERIFIED,
                message,
                {"matches": len(matches)},
            )
        elif intent.intent_name == "informational":
            result = self._informational(command, text)
        else:
            result = ToolResult(
                command.command_id, Status.UNVERIFIED, "No verified local action selected."
            )

        if record_original:
            self.db.record(command, result)
        self._remember_turn(command, intent.intent_name, result)
        return result

    async def observe_world(
        self,
        entity: str,
        attribute: str,
        value: object,
        *,
        confidence: float = 1.0,
        source: str = "runtime",
        importance: float = 0.5,
        message: str | None = None,
    ) -> WorldDelta:
        if self.world_model is None:
            raise RuntimeError("world model is unavailable")
        delta = self.world_model.observe(
            entity, attribute, value, confidence=confidence, source=source
        )
        await self.events.publish(
            EventEnvelope(
                "world.delta",
                {
                    "entity": delta.entity,
                    "attribute": delta.attribute,
                    "previous": delta.previous,
                    "current": delta.current,
                    "changed": delta.changed,
                    "confidence": delta.confidence,
                    "source": delta.source,
                    "importance": importance,
                    "message": message or f"{entity} {attribute} changed.",
                },
            )
        )
        return delta

    def discover_applications(self) -> list[ApplicationRecord]:
        return self.application_control.discover()

    def refresh_applications(self) -> list[ApplicationRecord]:
        return self.application_control.discover()

    def list_applications(self, include_non_user: bool = False) -> list[ApplicationRecord]:
        return self.application_control.catalog.list(include_non_user=include_non_user)

    def resolve_application(self, name: str) -> ResolutionResult:
        return self.application_control.resolve(name)

    def open_application(self, name: str) -> ApplicationOperationResult:
        return self.application_control.operate("open", name)

    def application_status(self, name: str) -> ApplicationOperationResult:
        return self.application_control.operate("status", name)

    def list_application_windows(self, name: str) -> ApplicationWindowsResult:
        resolved = self.resolve_application(name)
        if resolved.status != "RESOLVED" or resolved.selected_application is None:
            return ApplicationWindowsResult(resolved.status, detail="application not found")
        app = resolved.selected_application
        observed = self.application_control.observe(app)
        if not observed.processes:
            return ApplicationWindowsResult(
                ResolutionStatus.RESOLVED, app.application_id, detail="application not running"
            )
        return ApplicationWindowsResult(
            ResolutionStatus.RESOLVED,
            app.application_id,
            observed.windows,
            "visible windows found"
            if observed.windows
            else "application running with zero visible windows",
        )

    def focus_application(
        self, name: str, window_handle: int | None = None
    ) -> ApplicationOperationResult:
        return self.application_control.operate("focus", name, window_handle=window_handle)

    def minimize_application(
        self, name: str, window_handle: int | None = None
    ) -> ApplicationOperationResult:
        return self.application_control.operate("minimize", name, window_handle=window_handle)

    def maximize_application(
        self, name: str, window_handle: int | None = None
    ) -> ApplicationOperationResult:
        return self.application_control.operate("maximize", name, window_handle=window_handle)

    def restore_application(
        self, name: str, window_handle: int | None = None
    ) -> ApplicationOperationResult:
        return self.application_control.operate("restore", name, window_handle=window_handle)

    def close_application(
        self, name: str, approval_token: str | None = None, window_handle: int | None = None
    ) -> ApplicationOperationResult:
        return self.application_control.operate(
            "close", name, approval_token, window_handle=window_handle
        )

    def restart_application(
        self, name: str, approval_token: str | None = None
    ) -> ApplicationOperationResult:
        return self.application_control.operate("restart", name, approval_token)

    def system_audio(
        self, operation: str, value: int | bool | None = None, actor: str = "default"
    ) -> SystemControlResult:
        return self.system_control.audio(operation, value, actor)

    def system_brightness(
        self,
        operation: str,
        value: int | None = None,
        selector: str | None = None,
        actor: str = "default",
    ) -> SystemControlResult:
        return self.system_control.brightness(operation, value, selector, actor)


def build_runtime(root: Path) -> Runtime:
    from .runtime_builder import RuntimeBuilder

    container = RuntimeBuilder(root).build()
    return container.runtime
