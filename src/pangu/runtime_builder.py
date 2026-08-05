from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .applications import (
    ApplicationCatalog,
    ApplicationControlRuntime,
    ApplicationResolver,
    RealWindowsApplicationAdapter,
    WindowsApplicationAdapter,
)
from .approvals import PersistentApprovalService
from .capabilities import CapabilityCatalog, ToolSpecification
from .contracts import Risk
from .database import DatabaseService
from .events import EventBus
from .language import LanguageRuntime
from .lifecycle import LifecycleKernel
from .model_runtime import (
    CircuitBreaker,
    CloudContextSanitizer,
    CognitiveEngine,
    ContextAssembler,
    DeterministicProvider,
    GeminiProvider,
    GoogleGenAITransport,
    ModelBudget,
    ModelCapability,
    ModelCapabilityRegistry,
    ModelRole,
    ModelRouter,
    RetryPolicy,
)
from .permissions import PermissionGrant, PermissionStore
from .settings import PanguSettings, resolve_application_root

if TYPE_CHECKING:
    from .runtime import Runtime


@dataclass
class ServiceContainer:
    root: Path
    settings: PanguSettings
    database: DatabaseService
    lifecycle: LifecycleKernel
    events: EventBus
    catalog: CapabilityCatalog
    sanitizer: CloudContextSanitizer
    circuit_breaker: CircuitBreaker
    model_budget: ModelBudget
    deterministic_provider: DeterministicProvider
    gemini_provider: GeminiProvider
    model_router: ModelRouter
    cognitive_engine: CognitiveEngine
    language: LanguageRuntime
    context: ContextAssembler
    model_capabilities: ModelCapabilityRegistry
    application_adapter: WindowsApplicationAdapter
    application_catalog: ApplicationCatalog
    application_resolver: ApplicationResolver
    application_control: ApplicationControlRuntime
    runtime: Runtime = field(init=False)


class RuntimeBuilder:
    """The sole composition root; constructors perform no startup work."""

    def __init__(
        self, root: Path | None = None, application_adapter: WindowsApplicationAdapter | None = None
    ) -> None:
        self._root = root.resolve() if root is not None else resolve_application_root()
        self._application_adapter = application_adapter

    def build(self) -> ServiceContainer:
        settings = PanguSettings.load_root(self._root)
        database = DatabaseService(self._root / "runtime-data" / "database" / "pangu.db")
        sanitizer = CloudContextSanitizer()
        deterministic = DeterministicProvider()
        api_key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None
        models = {
            ModelRole.FAST: settings.gemini_fast_model,
            ModelRole.PRIMARY: settings.gemini_primary_model,
            ModelRole.CODING: settings.gemini_coding_model,
            ModelRole.VISION: settings.gemini_vision_model,
        }
        gemini = GeminiProvider(
            api_key,
            transport=GoogleGenAITransport(api_key) if api_key else None,
            models=models,
            circuit_breaker=CircuitBreaker(),
            retry_policy=RetryPolicy(settings.gemini_max_retries),
            budget_manager=ModelBudget(
                settings.gemini_max_model_calls_per_mission,
                settings.gemini_max_input_tokens_per_mission,
                settings.gemini_max_output_tokens_per_mission,
            ),
            sanitizer=sanitizer,
        )
        capabilities = ModelCapabilityRegistry()
        capabilities.register(ModelCapability("deterministic", "local-rules", ModelRole.FAST))
        for role, model in models.items():
            capabilities.register(
                ModelCapability("gemini", model, role, vision=role == ModelRole.VISION)
            )
        catalog = CapabilityCatalog()
        catalog.register(
            ToolSpecification(
                "filesystem",
                "1.0.0",
                frozenset({"create_folder", "write_text"}),
                Risk.LOW,
                frozenset({"filesystem.write:*"}),
            )
        )
        for tool_id, operations in {
            "application.discovery": {"read"},
            "application.catalog": {"read", "refresh"},
            "application.alias": {"read", "write"},
            "application.control": {"open", "focus", "close", "restart"},
            "application.window": {"list", "minimize", "maximize", "restore"},
            "application.process": {"terminate"},
        }.items():
            catalog.register(
                ToolSpecification(
                    tool_id,
                    "1.0.0",
                    frozenset(operations),
                    Risk.READ_ONLY if operations <= {"read", "list"} else Risk.LOW,
                    frozenset(),
                )
            )
        adapter = self._application_adapter or RealWindowsApplicationAdapter()
        app_catalog = ApplicationCatalog(database, adapter)
        app_resolver = ApplicationResolver(app_catalog)
        app_control = ApplicationControlRuntime(
            app_catalog,
            app_resolver,
            adapter,
            PermissionStore((PermissionGrant("application.control:*", "default"),)),
            PersistentApprovalService(database),
        )
        catalog.register(
            ToolSpecification(
                "system", "1.0.0", frozenset({"battery_status"}), Risk.READ_ONLY, frozenset()
            )
        )
        container = ServiceContainer(
            self._root,
            settings,
            database,
            LifecycleKernel(),
            EventBus(),
            catalog,
            sanitizer,
            gemini.circuit,
            gemini.budget,
            deterministic,
            gemini,
            ModelRouter(deterministic, gemini, sanitizer),
            CognitiveEngine(),
            LanguageRuntime(),
            ContextAssembler(),
            capabilities,
            adapter,
            app_catalog,
            app_resolver,
            app_control,
        )
        from .runtime import Runtime

        container.runtime = Runtime(
            container.root,
            container.settings,
            container.database,
            container.lifecycle,
            container.events,
            container.catalog,
            container.language,
            container.context,
            container.model_router,
            container.cognitive_engine,
            container.application_control,
        )
        return container
