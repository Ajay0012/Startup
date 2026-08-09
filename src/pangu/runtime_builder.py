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
from .gestures import GestureRuntime, MediaPipeHandTracker, TemporalGestureRecognizer
from .language import LanguageRuntime
from .lifecycle import LifecycleKernel, LifecycleService
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
from .production_voice import ProductionVoiceSessionRuntime, WakePhrasePolicyVerifier
from .realtime_voice import RealtimeVoiceTurnCoordinator
from .security import SafetyGateway
from .settings import PanguSettings, resolve_application_root
from .system_control import SystemControlAdapter, SystemControlRuntime, WindowsSystemControlAdapter
from .tts import WindowsSapiSpeechProvider
from .voice import VadActivationService, VoiceSessionRuntime, WindowsAudioInputAdapter
from .voice_providers import FasterWhisperTranscriptionProvider
from .wake_word import SherpaKeywordSpotterWakeWordEngine, load_wake_word_config

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
    system_control: SystemControlRuntime
    voice: VoiceSessionRuntime
    gesture: GestureRuntime
    runtime: Runtime = field(init=False)
    realtime_voice: RealtimeVoiceTurnCoordinator | None = field(init=False, default=None)


class RuntimeBuilder:
    """The sole composition root; constructors perform no startup work."""

    def __init__(
        self,
        root: Path | None = None,
        application_adapter: WindowsApplicationAdapter | None = None,
        system_control_adapter: SystemControlAdapter | None = None,
        voice_runtime: VoiceSessionRuntime | None = None,
    ) -> None:
        self._root = root.resolve() if root is not None else resolve_application_root()
        self._application_adapter = application_adapter
        self._system_control_adapter = system_control_adapter
        self._voice_runtime = voice_runtime

    def build(self) -> ServiceContainer:
        settings = PanguSettings.load_root(self._root)
        database = DatabaseService(self._root / "runtime-data" / "database" / "pangu.db")
        events = EventBus()
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
        catalog.register(
            ToolSpecification(
                "system.control",
                "1.0.0",
                frozenset(
                    {
                        "get_volume",
                        "set_volume",
                        "increase_volume",
                        "decrease_volume",
                        "get_mute_state",
                        "mute",
                        "unmute",
                        "toggle_mute",
                        "get_brightness",
                        "set_brightness",
                        "increase_brightness",
                        "decrease_brightness",
                    }
                ),
                Risk.LOW,
                frozenset(),
            )
        )
        system_permissions = PermissionStore(
            (
                PermissionGrant("system.audio.read", "default"),
                PermissionGrant("system.audio.write", "default"),
                PermissionGrant("system.brightness.read", "default"),
                PermissionGrant("system.brightness.write", "default"),
            )
        )
        system_control = SystemControlRuntime(
            self._system_control_adapter or WindowsSystemControlAdapter(),
            catalog,
            system_permissions,
            SafetyGateway(),
            database,
        )
        manifest_path = (
            Path(__file__).resolve().parents[2]
            / "models"
            / "voice"
            / "vad"
            / "silero"
            / "v4"
            / "manifest.json"
        )
        vad = VadActivationService(self._root / "models", manifest_path).activate()
        transcriber = FasterWhisperTranscriptionProvider(
            self._root / "models" / "voice" / "whisper"
        )
        wake = SherpaKeywordSpotterWakeWordEngine(
            load_wake_word_config(self._root, settings.pangu_wake_cooldown_seconds)
        )
        voice = self._voice_runtime or ProductionVoiceSessionRuntime(
            WindowsAudioInputAdapter(),
            vad,
            wake,
            WakePhrasePolicyVerifier(),
            transcriber,
            events,
            LanguageRuntime(),
        )
        gesture = GestureRuntime(
            MediaPipeHandTracker(
                self._root / settings.pangu_gesture_model_path,
                camera_index=settings.pangu_gesture_camera_index,
                max_hands=2,
            ),
            TemporalGestureRecognizer(),
            events,
        )
        container = ServiceContainer(
            self._root,
            settings,
            database,
            LifecycleKernel(),
            events,
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
            system_control,
            voice,
            gesture,
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
            container.system_control,
            container.voice,
        )
        if isinstance(container.voice, ProductionVoiceSessionRuntime):
            container.realtime_voice = RealtimeVoiceTurnCoordinator(
                container.voice,
                container.runtime,
                container.events,
                WindowsSapiSpeechProvider(),
            )
            container.lifecycle.register(
                LifecycleService(
                    "realtime_voice",
                    container.realtime_voice.start,
                    container.realtime_voice.stop,
                    ("events", "voice"),
                )
            )
        if settings.pangu_gestures_enabled:
            container.lifecycle.register(
                LifecycleService("gesture", container.gesture.start, container.gesture.stop, ("events",))
            )
        return container
