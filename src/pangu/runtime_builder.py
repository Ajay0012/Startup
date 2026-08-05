from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .capabilities import CapabilityCatalog, ToolSpecification
from .contracts import Risk
from .database import DatabaseService
from .events import EventBus
from .lifecycle import LifecycleKernel
from .language import LanguageRuntime
from .model_runtime import (
    CircuitBreaker,
    CloudContextSanitizer,
    CognitiveEngine,
    ContextAssembler,
    DeterministicProvider,
    GeminiProvider,
    ModelBudget,
    ModelRouter,
)
from .settings import PanguSettings

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
    runtime: Runtime = field(init=False)


class RuntimeBuilder:
    """The sole composition root; constructors perform no startup work."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def build(self) -> ServiceContainer:
        settings = PanguSettings.load_root(self._root)
        database = DatabaseService(self._root / "runtime-data" / "database" / "pangu.db")
        sanitizer = CloudContextSanitizer()
        deterministic = DeterministicProvider()
        gemini = GeminiProvider(
            settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None,
            settings.gemini_primary_model,
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
            CircuitBreaker(),
            ModelBudget(
                settings.gemini_max_model_calls_per_mission,
                settings.gemini_max_input_tokens_per_mission,
                settings.gemini_max_output_tokens_per_mission,
            ),
            deterministic,
            gemini,
            ModelRouter(deterministic, gemini, sanitizer),
            CognitiveEngine(),
            LanguageRuntime(),
            ContextAssembler(),
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
        )
        return container
