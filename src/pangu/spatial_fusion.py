from __future__ import annotations

from dataclasses import dataclass

from .multimodal import ContextSignal, GroundedReferent, Modality, MultimodalContextFusion
from .spatial_interaction import SpatialInteractionProposal


@dataclass(frozen=True)
class SpatiallyGroundedAction:
    proposal: SpatialInteractionProposal
    referent: GroundedReferent | None
    requires_disambiguation: bool
    execute_directly: bool = False


class GestureHudFusionRuntime:
    """Bind gestures to recent HUD/screen targets without directly executing them."""

    def __init__(self, fusion: MultimodalContextFusion) -> None:
        self.fusion = fusion

    def observe_target(
        self,
        *,
        target_id: str,
        label: str,
        source: Modality,
        confidence: float,
    ) -> None:
        self.fusion.observe(
            ContextSignal(
                source,
                "spatial_target",
                label,
                confidence,
                target_id=target_id,
                source="gesture-hud-fusion",
            )
        )

    def ground(
        self,
        proposal: SpatialInteractionProposal,
        utterance: str = "this",
    ) -> SpatiallyGroundedAction:
        referent = self.fusion.resolve_referent(utterance)
        requires_target = proposal.operation.value in {
            "POINTER_MOVE",
            "SELECT",
            "GRAB_BEGIN",
            "RELEASE",
            "SCALE",
            "ROTATE",
        }
        return SpatiallyGroundedAction(
            proposal,
            referent,
            requires_target and referent is None,
            False,
        )
