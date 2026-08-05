from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .capabilities import CapabilityCatalog
from .database import DatabaseService
from .events import EventBus
from .lifecycle import LifecycleKernel
from .model_runtime import (
    CircuitBreaker,
    CloudContextSanitizer,
    CognitiveEngine,
    DeterministicProvider,
    GeminiProvider,
    ModelBudget,
    ModelRouter,
)
from .settings import PanguSettings


@dataclass(frozen=True)
class ServiceContainer:
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
        return ServiceContainer(
            settings,
            database,
            LifecycleKernel(),
            EventBus(),
            CapabilityCatalog(),
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
        )
