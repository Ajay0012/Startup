from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contextual_nlu import ContextualLanguageResolver
from .evaluation import IntelligenceEvaluationGate
from .hud_bridge import HudStateBridge
from .memory import PersistentMemoryRuntime
from .multimodal import MultimodalContextFusion
from .predictive_intelligence import PredictiveBehaviorRuntime
from .procedure_learning import ProcedureLearningRuntime
from .proactive_intelligence import ContextualInterruptionPolicy
from .resilience import ResilientLoadManager, SelfHealingSupervisor
from .speaker_identity import IdentityTrustEngine, SpeakerIdentityRuntime
from .spatial_fusion import GestureHudFusionRuntime
from .windows_extended import ExtendedWindowsRuntime
from .world_graph import PersonalWorldGraph
from .world_model import PersonalWorldModel
from .events import EventBus


@dataclass
class AdvancedIntelligenceServices:
    """Single composition bundle for enhanced PANGU intelligence services."""

    multimodal: MultimodalContextFusion
    contextual_language: ContextualLanguageResolver
    world_graph: PersonalWorldGraph
    procedures: ProcedureLearningRuntime
    predictive: PredictiveBehaviorRuntime
    evaluation: IntelligenceEvaluationGate
    speaker_identity: SpeakerIdentityRuntime
    identity_trust: IdentityTrustEngine
    interruption_policy: ContextualInterruptionPolicy
    gesture_hud: GestureHudFusionRuntime
    windows_extended: ExtendedWindowsRuntime
    self_healing: SelfHealingSupervisor
    model_load: ResilientLoadManager[object]
    browser_load: ResilientLoadManager[object]
    perception_load: ResilientLoadManager[object]
    hud: HudStateBridge


def build_advanced_intelligence(
    root: Path,
    events: EventBus,
    memory: PersistentMemoryRuntime,
    world_model: PersonalWorldModel,
) -> AdvancedIntelligenceServices:
    """Build enhanced services without starting hardware or creating parallel owners."""

    multimodal = MultimodalContextFusion(max_signals=256, half_life_seconds=8.0)
    return AdvancedIntelligenceServices(
        multimodal=multimodal,
        contextual_language=ContextualLanguageResolver(multimodal),
        world_graph=PersonalWorldGraph(world_model),
        procedures=ProcedureLearningRuntime(memory),
        predictive=PredictiveBehaviorRuntime(history_limit=1024, minimum_support=3),
        evaluation=IntelligenceEvaluationGate(allowed_relative_regression=0.02),
        speaker_identity=SpeakerIdentityRuntime(),
        identity_trust=IdentityTrustEngine(),
        interruption_policy=ContextualInterruptionPolicy(),
        gesture_hud=GestureHudFusionRuntime(multimodal),
        windows_extended=ExtendedWindowsRuntime(),
        self_healing=SelfHealingSupervisor(probe_timeout_seconds=3.0),
        model_load=ResilientLoadManager(
            ["gemini-primary"], max_concurrency=4, max_queue=32
        ),
        browser_load=ResilientLoadManager(
            ["browser"], max_concurrency=3, max_queue=20
        ),
        perception_load=ResilientLoadManager(
            ["screen", "camera"],
            max_concurrency=2,
            max_queue=8,
            endpoint_weights={"screen": 2, "camera": 1},
        ),
        hud=HudStateBridge(events, root / "runtime-data" / "overlay" / "state.json"),
    )
