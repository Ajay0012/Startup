from __future__ import annotations

import asyncio
from pathlib import Path

from .capabilities import CapabilityCatalog
from .contracts import CommandEnvelope, Status, ToolRequest, ToolResult
from .database import DatabaseService
from .events import EventBus
from .language import LanguageRuntime
from .lifecycle import LifecycleKernel, LifecycleService
from .model_runtime import CognitiveDecision, CognitiveEngine, ContextAssembler, ModelRouter
from .permissions import PermissionGrant, PermissionStore
from .security import ApprovalStore, SafetyGateway
from .tools import ToolRuntime


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
        grants = PermissionStore((PermissionGrant("filesystem.write:*", "default"),))
        self.approvals = ApprovalStore()
        self.tools = ToolRuntime(root, self.safety, self.catalog, grants, self.approvals)
        self.started = False

        async def start_database() -> None:
            self.db.start()

        async def stop_database() -> None:
            self.db.stop()

        self.lifecycle.register(LifecycleService("database", start_database, stop_database))

    def start(self) -> None:
        asyncio.run(self.start_async())

    async def start_async(self) -> None:
        await self.lifecycle.start()
        self.started = self.db.health_details()["database_ready"] is True

    def stop(self) -> None:
        asyncio.run(self.stop_async())

    async def stop_async(self) -> None:
        await self.lifecycle.stop()
        self.started = False

    def decide(self, text: str) -> CognitiveDecision:
        intent = self.language.normalize(text)
        self.context.assemble(intent.canonical_english)
        deterministic = intent.intent_name in {
            "create_folder",
            "battery_status",
            "open_application",
            "mute_volume",
            "volume_down",
        }
        route = self.model_router.route(intent.canonical_english, deterministic)
        return self.cognitive_engine.decide(intent.intent_name, route, intent.original_text)

    def command(self, text: str, source: str = "cli") -> ToolResult:
        if not self.started:
            raise RuntimeError("runtime not started")
        command = CommandEnvelope(text, source)
        intent = self.language.normalize(text)
        if intent.intent_name == "create_folder":
            request = ToolRequest(
                "filesystem", "create_folder", {"name": intent.entities.get("name", "New Folder")}
            )
        elif intent.intent_name == "battery_status":
            request = ToolRequest("system", "battery_status", {})
        else:
            result = ToolResult(
                command.command_id, Status.UNVERIFIED, "No deterministic action selected."
            )
            self.db.record(command, result)
            return result
        result = self.tools.execute(request)
        self.db.record(command, result)
        return result


def build_runtime(root: Path) -> Runtime:
    from .runtime_builder import RuntimeBuilder

    container = RuntimeBuilder(root).build()
    return container.runtime
