from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .gestures import GestureDetection, GestureKind


class SpatialAction(StrEnum):
    POINTER_MOVE = "POINTER_MOVE"
    SELECT = "SELECT"
    GRAB_BEGIN = "GRAB_BEGIN"
    RELEASE = "RELEASE"
    NAVIGATE_LEFT = "NAVIGATE_LEFT"
    NAVIGATE_RIGHT = "NAVIGATE_RIGHT"
    NAVIGATE_UP = "NAVIGATE_UP"
    NAVIGATE_DOWN = "NAVIGATE_DOWN"
    SCALE = "SCALE"
    ROTATE = "ROTATE"


@dataclass(frozen=True)
class SpatialActionProposal:
    action: SpatialAction
    hand_ids: tuple[str, ...]
    confidence: float
    parameters: dict[str, float | str]
    requires_target_resolution: bool = True
    requires_approval: bool = False


@dataclass
class SpatialInteractionState:
    pointer_x: float | None = None
    pointer_y: float | None = None
    grabbed: bool = False
    grabbed_hand_id: str | None = None
    recent_actions: list[SpatialAction] = field(default_factory=list)


class SpatialInteractionController:
    """Transforms gestures into proposals; it never performs OS input directly."""

    def __init__(self) -> None:
        self.state = SpatialInteractionState()

    def propose(self, detection: GestureDetection) -> SpatialActionProposal | None:
        gesture = detection.gesture
        metadata = detection.metadata
        proposal: SpatialActionProposal | None = None

        if gesture == GestureKind.POINT:
            x = float(metadata.get("x", 0.0))
            y = float(metadata.get("y", 0.0))
            self.state.pointer_x = x
            self.state.pointer_y = y
            proposal = SpatialActionProposal(
                SpatialAction.POINTER_MOVE,
                detection.hand_ids,
                detection.confidence,
                {"x": x, "y": y},
                requires_target_resolution=False,
            )
        elif gesture == GestureKind.PINCH:
            proposal = SpatialActionProposal(
                SpatialAction.SELECT,
                detection.hand_ids,
                detection.confidence,
                {
                    "x": self.state.pointer_x if self.state.pointer_x is not None else 0.0,
                    "y": self.state.pointer_y if self.state.pointer_y is not None else 0.0,
                },
            )
        elif gesture == GestureKind.GRAB:
            self.state.grabbed = True
            self.state.grabbed_hand_id = detection.hand_ids[0] if detection.hand_ids else None
            proposal = SpatialActionProposal(
                SpatialAction.GRAB_BEGIN,
                detection.hand_ids,
                detection.confidence,
                {},
            )
        elif gesture == GestureKind.OPEN_PALM and self.state.grabbed:
            self.state.grabbed = False
            self.state.grabbed_hand_id = None
            proposal = SpatialActionProposal(
                SpatialAction.RELEASE,
                detection.hand_ids,
                detection.confidence,
                {},
            )
        elif gesture in {
            GestureKind.SWIPE_LEFT,
            GestureKind.SWIPE_RIGHT,
            GestureKind.SWIPE_UP,
            GestureKind.SWIPE_DOWN,
        }:
            mapping = {
                GestureKind.SWIPE_LEFT: SpatialAction.NAVIGATE_LEFT,
                GestureKind.SWIPE_RIGHT: SpatialAction.NAVIGATE_RIGHT,
                GestureKind.SWIPE_UP: SpatialAction.NAVIGATE_UP,
                GestureKind.SWIPE_DOWN: SpatialAction.NAVIGATE_DOWN,
            }
            proposal = SpatialActionProposal(
                mapping[gesture],
                detection.hand_ids,
                detection.confidence,
                dict(metadata),
                requires_target_resolution=False,
            )
        elif gesture in {GestureKind.TWO_HAND_SCALE_IN, GestureKind.TWO_HAND_SCALE_OUT}:
            proposal = SpatialActionProposal(
                SpatialAction.SCALE,
                detection.hand_ids,
                detection.confidence,
                {"scale_delta": float(metadata.get("scale_delta", 0.0))},
            )
        elif gesture in {
            GestureKind.TWO_HAND_ROTATE_CLOCKWISE,
            GestureKind.TWO_HAND_ROTATE_COUNTERCLOCKWISE,
        }:
            proposal = SpatialActionProposal(
                SpatialAction.ROTATE,
                detection.hand_ids,
                detection.confidence,
                {"rotation_degrees": float(metadata.get("rotation_degrees", 0.0))},
            )

        if proposal is not None:
            self.state.recent_actions.append(proposal.action)
            del self.state.recent_actions[:-32]
        return proposal
