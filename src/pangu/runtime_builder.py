from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .advanced_language import ContextAwareLanguageRuntime
from .advanced_realtime_voice import AdvancedRealtimeVoiceTurnCoordinator
from .advanced_services import AdvancedIntelligenceServices, build_advanced_intelligence
from .applications import (
    ApplicationCatalog,
    ApplicationControlRuntime,
    ApplicationResolver,
    RealWindowsApplicationAdapter,
    WindowsApplicationAdapter,
)
from .approvals import PersistentApprovalService
from .awareness import ProactiveAwarenessRuntime
from .browser import BrowserRuntime, PlaywrightBrowserAdapter
from .capabilities import CapabilityCatalog, ToolSpecification
from .computer_use import ComputerUseRuntime
from .contracts import Risk
from .database import DatabaseService
from .events import EventBus
from .gestures import GestureRuntime, MediaPipeHandTracker, TemporalGestureRecognizer
from .hardened_runtime import HardenedRuntime
from .language import LanguageRuntime
from .lifecycle import LifecycleKernel, LifecycleService
from .memory import PersistentMemoryRuntime
from .missions import PersistentMissionRuntime
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
from .phone_link import PhoneLinkRuntime
from .phone_service import PhoneIntelligenceService
from .production_voice import ProductionVoiceSessionRuntime, WakePhrasePolicyVerifier
from .realtime_voice import RealtimeVoiceTurnCoordinator
from .screen_perception import ScreenPerceptionRuntime
from .security import SafetyGateway
from .settings import PanguSettings, resolve_application_root
from .system_awareness import SystemAwarenessRuntime
from .system_control import SystemControlAdapter, SystemControlRuntime, WindowsSystemControlAdapter
from .tts import WindowsSapiSpeechProvider
from .voice import VadActivationService, VoiceSessionRuntime, WindowsAudioInputAdapter
from .voice_providers import FasterWhisperTranscriptionProvider
from .wake_word import SherpaKeywordSpotterWakeWordEngine, load_wake_word_config
from .world_model import PersonalWorldModel

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
    approvals: PersistentApprovalService
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
    memory: PersistentMemoryRuntime
    world_model: PersonalWorldModel
    missions: PersistentMissionRuntime
    awareness: ProactiveAwarenessRuntime
    system_awareness: SystemAwarenessRuntime
    screen: ScreenPerceptionRuntime
    computer_use: ComputerUseRuntime
    browser: BrowserRuntime
    phone_link: PhoneLinkRuntime
    phone: PhoneIntelligenceService
    advanced: AdvancedIntelligenceServices
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
        approvals = PersistentApprovalService(database)
        events = EventBus()
        memory = PersistentMemoryRuntime(database)
        world_model = PersonalWorldModel(database)
        missions = PersistentMissionRuntime(database, events)
        awareness = ProactiveAwarenessRuntime(events, memory)
        system_awareness = SystemAwarenessRuntime(
            world_model,
            events,
            interval_seconds=settings.pangu_awareness_interval_seconds,
        )
        screen = ScreenPerceptionRuntime()
        computer_use = ComputerUseRuntime(screen)
        browser = BrowserRuntime(
            PlaywrightBrowserAdapter(
                self._root / "runtime-data" / "browser" / "profile",
                headless=settings.pangu_browser_headless,
            )
        )
        phone_secret = (
            settings.pangu_phone_pairing_secret.get_secret_value()
            if settings.pangu_phone_pairing_secret
            else None
        )
        phone_link = PhoneLinkRuntime(
            phone_secret if settings.pangu_phone_enabled else None,
            command_ttl_seconds=settings.pangu_phone_command_ttl_seconds,
        )
        phone = PhoneIntelligenceService(phone_link, events)
        advanced = build_advanced_intelligence(
            self._root,
            events,
            memory,
            world_model,
            screen,
            screen_observation_enabled=settings.pangu_screen_observation_enabled,
            screen_observation_interval_seconds=settings.pangu_screen_observation_interval_seconds,
            screen_observation_ocr_enabled=settings.pangu_screen_observation_ocr_enabled,
            screen_observation_suppress_password_contexts=(
                settings.pangu_screen_observation_suppress_password_contexts
            ),
        )
        language = ContextAwareLanguageRuntime(advanced.contextual_language)
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
            approvals,
        )

        catalog.register(
            ToolSpecification(
                "system",
                "1.0.0",
                frozenset({"battery_status"}),
                Risk.READ_ONLY,
                frozenset(),
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
            language,
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
            root=self._root,
            settings=settings,
            database=database,
            lifecycle=LifecycleKernel(),
            events=events,
            catalog=catalog,
            approvals=approvals,
            sanitizer=sanitizer,
            circuit_breaker=gemini.circuit,
            model_budget=gemini.budget,
            deterministic_provider=deterministic,
            gemini_provider=gemini,
            model_router=ModelRouter(deterministic, gemini, sanitizer),
            cognitive_engine=CognitiveEngine(),
            language=language,
            context=ContextAssembler(),
            model_capabilities=capabilities,
            application_adapter=adapter,
            application_catalog=app_catalog,
            application_resolver=app_resolver,
            application_control=app_control,
            system_control=system_control,
            voice=voice,
            gesture=gesture,
            memory=memory,
            world_model=world_model,
            missions=missions,
            awareness=awareness,
            system_awareness=system_awareness,
            screen=screen,
            computer_use=computer_use,
            browser=browser,
            phone_link=phone_link,
            phone=phone,
            advanced=advanced,
        )

        container.runtime = HardenedRuntime(
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
            container.memory,
            container.world_model,
            container.missions,
            container.screen,
            container.computer_use,
            container.browser,
            root=container.root,
            persistent_approvals=container.approvals,
        )
        container.lifecycle.register(
            LifecycleService(
                "awareness",
                container.awareness.start,
                container.awareness.stop,
                ("database", "events"),
            )
        )
        container.lifecycle.register(
            LifecycleService(
                "hud_bridge",
                container.advanced.hud.start,
                container.advanced.hud.stop,
                ("events",),
            )
        )
        container.lifecycle.register(
            LifecycleService(
                "resilience",
                container.advanced.resilience.start,
                container.advanced.resilience.stop,
                ("events",),
            )
        )
        if settings.pangu_screen_observation_enabled:
            container.lifecycle.register(
                LifecycleService(
                    "screen_observer",
                    container.advanced.screen_observer.start,
                    container.advanced.screen_observer.stop,
                    ("events",),
                )
            )
        if settings.pangu_awareness_enabled:
            container.lifecycle.register(
                LifecycleService(
                    "system_awareness",
                    container.system_awareness.start,
                    container.system_awareness.stop,
                    ("database", "events", "awareness"),
                )
            )
        if settings.pangu_browser_enabled or settings.pangu_media_enabled:
            container.lifecycle.register(
                LifecycleService(
                    "browser",
                    container.browser.start,
                    container.browser.stop,
                    ("events",),
                )
            )
        if isinstance(container.voice, ProductionVoiceSessionRuntime):
            coordinator_type = (
                AdvancedRealtimeVoiceTurnCoordinator
                if settings.pangu_full_duplex_voice_enabled
                else RealtimeVoiceTurnCoordinator
            )
            container.realtime_voice = coordinator_type(
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
                LifecycleService(
                    "gesture",
                    container.gesture.start,
                    container.gesture.stop,
                    ("events",),
                )
            )
        return container
