from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import hypot

from .gestures import GestureDetection, GestureKind


class SpatialAction(StrEnum):
    POINTER_MOVE = "POINTER_MOVE"
    HOVER_TARGET = "HOVER_TARGET"
    SELECT = "SELECT"
    GRAB_BEGIN = "GRAB_BEGIN"
    DRAG = "DRAG"
    RELEASE = "RELEASE"
    THROW_TO_TRASH = "THROW_TO_TRASH"
    NAVIGATE_LEFT = "NAVIGATE_LEFT"
    NAVIGATE_RIGHT = "NAVIGATE_RIGHT"
    NAVIGATE_UP = "NAVIGATE_UP"
    NAVIGATE_DOWN = "NAVIGATE_DOWN"
    SCALE = "SCALE"
    ROTATE = "ROTATE"


@dataclass(frozen=True)
class SemanticTarget:
    """A normalized, semantic target supplied by a browser/UI adapter.

    Spatial interaction never performs OS input itself. Destructive operations are
    proposals only and must still pass target re-resolution and approval boundaries.
    """

    target_id: str
    kind: str
    x: float
    y: float
    width: float
    height: float
    closable: bool = False
    destructive: bool = False
    unsaved: bool = False
    selection_count: int = 1

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x + self.width and self.y <= y <= self.y + self.height


@dataclass(frozen=True)
class TrashZone:
    x: float = 0.82
    y: float = 0.72
    width: float = 0.16
    height: float = 0.22

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x + self.width and self.y <= y <= self.y + self.height


@dataclass(frozen=True)
class SpatialActionProposal:
    action: SpatialAction
    hand_ids: tuple[str, ...]
    confidence: float
    parameters: dict[str, float | str | bool | int]
    requires_target_resolution: bool = True
    requires_approval: bool = False


@dataclass
class SpatialInteractionState:
    pointer_x: float | None = None
    pointer_y: float | None = None
    grabbed: bool = False
    grabbed_hand_id: str | None = None
    grabbed_target_id: str | None = None
    recent_actions: list[SpatialAction] = field(default_factory=list)
    trajectory: list[tuple[float, float, float]] = field(default_factory=list)


class SpatialInteractionController:
    """Transforms gestures into safe proposals; it never performs OS input directly.

    A deliberate fist can air-grab the preferred semantic target even when the
    pointer is not pixel-aligned with it. Callers should order targets by preference
    (for example, active Chrome tab first). A sufficiently fast release is treated as
    a throw-to-close proposal regardless of screen region, so the user does not need
    to aim at a trash zone. Actual closing remains the responsibility of a guarded
    execution layer after fresh target re-resolution.
    """

    def __init__(
        self,
        *,
        throw_velocity_threshold: float = 0.55,
        trajectory_window_seconds: float = 0.35,
    ) -> None:
        self.state = SpatialInteractionState()
        self.throw_velocity_threshold = throw_velocity_threshold
        self.trajectory_window_seconds = trajectory_window_seconds

    @staticmethod
    def _resolve_target(
        x: float,
        y: float,
        targets: tuple[SemanticTarget, ...],
    ) -> SemanticTarget | None:
        candidates = [target for target in targets if target.contains(x, y)]
        if not candidates:
            return None
        return min(candidates, key=lambda item: item.width * item.height)

    def _remember_pointer(self, timestamp: float, x: float, y: float) -> None:
        self.state.pointer_x = min(1.0, max(0.0, x))
        self.state.pointer_y = min(1.0, max(0.0, y))
        self.state.trajectory.append((timestamp, self.state.pointer_x, self.state.pointer_y))
        cutoff = timestamp - self.trajectory_window_seconds
        self.state.trajectory[:] = [item for item in self.state.trajectory if item[0] >= cutoff]

    def _release_velocity(self) -> tuple[float, float, float]:
        if len(self.state.trajectory) < 2:
            return 0.0, 0.0, 0.0
        start = self.state.trajectory[0]
        end = self.state.trajectory[-1]
        elapsed = end[0] - start[0]
        if elapsed <= 0:
            return 0.0, 0.0, 0.0
        vx = (end[1] - start[1]) / elapsed
        vy = (end[2] - start[2]) / elapsed
        return vx, vy, hypot(vx, vy)

    @staticmethod
    def _project(
        x: float, y: float, vx: float, vy: float, horizon: float = 0.18
    ) -> tuple[float, float]:
        return x + vx * horizon, y + vy * horizon

    def _record(self, proposal: SpatialActionProposal | None) -> SpatialActionProposal | None:
        if proposal is not None:
            self.state.recent_actions.append(proposal.action)
            del self.state.recent_actions[:-32]
        return proposal

    def propose(
        self,
        detection: GestureDetection,
        targets: tuple[SemanticTarget, ...] = (),
        trash_zone: TrashZone | None = None,
    ) -> SpatialActionProposal | None:
        gesture = detection.gesture
        metadata = detection.metadata
        proposal: SpatialActionProposal | None = None

        if gesture == GestureKind.POINT:
            x = float(metadata.get("x", 0.0))
            y = float(metadata.get("y", 0.0))
            self._remember_pointer(detection.timestamp, x, y)
            target = self._resolve_target(
                self.state.pointer_x or 0.0, self.state.pointer_y or 0.0, targets
            )
            if self.state.grabbed:
                proposal = SpatialActionProposal(
                    SpatialAction.DRAG,
                    detection.hand_ids,
                    detection.confidence,
                    {
                        "x": self.state.pointer_x or 0.0,
                        "y": self.state.pointer_y or 0.0,
                        "target_id": self.state.grabbed_target_id or "",
                    },
                    requires_target_resolution=True,
                )
            elif target is not None:
                proposal = SpatialActionProposal(
                    SpatialAction.HOVER_TARGET,
                    detection.hand_ids,
                    detection.confidence,
                    {
                        "x": self.state.pointer_x or 0.0,
                        "y": self.state.pointer_y or 0.0,
                        "target_id": target.target_id,
                    },
                    requires_target_resolution=True,
                )
            else:
                proposal = SpatialActionProposal(
                    SpatialAction.POINTER_MOVE,
                    detection.hand_ids,
                    detection.confidence,
                    {"x": self.state.pointer_x or 0.0, "y": self.state.pointer_y or 0.0},
                    requires_target_resolution=False,
                )

        elif gesture == GestureKind.PINCH:
            x = self.state.pointer_x if self.state.pointer_x is not None else 0.0
            y = self.state.pointer_y if self.state.pointer_y is not None else 0.0
            target = self._resolve_target(x, y, targets)
            proposal = SpatialActionProposal(
                SpatialAction.SELECT,
                detection.hand_ids,
                detection.confidence,
                {"x": x, "y": y, "target_id": target.target_id if target else ""},
            )

        elif gesture == GestureKind.GRAB:
            hand_id = detection.hand_ids[0] if detection.hand_ids else None
            x = self.state.pointer_x if self.state.pointer_x is not None else 0.0
            y = self.state.pointer_y if self.state.pointer_y is not None else 0.0
            target = self._resolve_target(x, y, targets)
            air_grab = target is None and bool(targets)
            if target is None and targets:
                target = targets[0]
            if target is not None:
                self.state.grabbed = True
                self.state.grabbed_hand_id = hand_id
                self.state.grabbed_target_id = target.target_id
                self.state.trajectory.clear()
                self._remember_pointer(detection.timestamp, x, y)
                proposal = SpatialActionProposal(
                    SpatialAction.GRAB_BEGIN,
                    detection.hand_ids,
                    detection.confidence,
                    {
                        "target_id": target.target_id,
                        "x": x,
                        "y": y,
                        "air_grab": air_grab,
                    },
                )

        elif gesture == GestureKind.OPEN_PALM and self.state.grabbed:
            x = self.state.pointer_x if self.state.pointer_x is not None else 0.0
            y = self.state.pointer_y if self.state.pointer_y is not None else 0.0
            target = next(
                (item for item in targets if item.target_id == self.state.grabbed_target_id), None
            )
            vx, vy, speed = self._release_velocity()
            projected_x, projected_y = self._project(x, y, vx, vy)
            throw_to_close = (
                target is not None
                and target.closable
                and speed >= self.throw_velocity_threshold
            )
            if throw_to_close:
                approval = bool(target.destructive or target.unsaved or target.selection_count > 1)
                proposal = SpatialActionProposal(
                    SpatialAction.THROW_TO_TRASH,
                    detection.hand_ids,
                    detection.confidence,
                    {
                        "target_id": target.target_id,
                        "target_kind": target.kind,
                        "selection_count": target.selection_count,
                        "unsaved": target.unsaved,
                        "velocity_x": vx,
                        "velocity_y": vy,
                        "speed": speed,
                        "projected_x": projected_x,
                        "projected_y": projected_y,
                        "throw_anywhere": True,
                    },
                    requires_target_resolution=True,
                    requires_approval=approval,
                )
            else:
                proposal = SpatialActionProposal(
                    SpatialAction.RELEASE,
                    detection.hand_ids,
                    detection.confidence,
                    {
                        "target_id": self.state.grabbed_target_id or "",
                        "x": x,
                        "y": y,
                        "speed": speed,
                    },
                )
            self.state.grabbed = False
            self.state.grabbed_hand_id = None
            self.state.grabbed_target_id = None
            self.state.trajectory.clear()

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

        return self._record(proposal)
