from __future__ import annotations

from math import hypot
from pathlib import Path

from .advanced_pointing import AdvancedPointingEstimator
from .gestures import GestureDetection, GestureKind, HandObservation
from .spatial_interaction import SpatialAction, SpatialActionProposal
from .spatial_live import GestureStabilizer, LiveSpatialDryRunRuntime


class AdvancedLiveSpatialDryRunRuntime(LiveSpatialDryRunRuntime):
    """Live dry-run runtime with ray pointing and robust manipulation poses."""

    def __init__(
        self,
        *,
        model_path: Path,
        hud_state_path: Path,
        camera_index: int = 0,
        mirror_x: bool = True,
        pointer_x_min: float = 0.12,
        pointer_x_max: float = 0.88,
        pointer_y_min: float = 0.12,
        pointer_y_max: float = 0.88,
        pointer_smoothing: float = 0.58,
        target_padding: float = 0.04,
        ray_gain: float = 0.22,
        snap_radius: float = 0.035,
        snap_strength: float = 0.62,
        throw_velocity_threshold: float = 0.22,
    ) -> None:
        super().__init__(
            model_path=model_path,
            hud_state_path=hud_state_path,
            camera_index=camera_index,
            mirror_x=mirror_x,
            pointer_x_min=pointer_x_min,
            pointer_x_max=pointer_x_max,
            pointer_y_min=pointer_y_min,
            pointer_y_max=pointer_y_max,
            pointer_smoothing=pointer_smoothing,
            target_padding=target_padding,
        )
        if not 0.05 <= throw_velocity_threshold <= 2.0:
            raise ValueError("throw_velocity_threshold must be between 0.05 and 2.0")
        self.advanced_pointer = AdvancedPointingEstimator(
            ray_gain=ray_gain,
            snap_radius=snap_radius,
            snap_strength=snap_strength,
        )
        self.spatial.throw_velocity_threshold = throw_velocity_threshold
        # A fast throw often exposes the palm for only a few video frames. Accept
        # release immediately while keeping GRAB and PINCH guarded against flicker.
        self.stabilizer = GestureStabilizer(
            grab_frames=2,
            open_palm_frames=1,
            pinch_frames=5,
        )
        self._throw_count = 0
        self._release_count = 0
        self._last_terminal_action: str | None = None
        self._last_release_speed = 0.0

    def _manipulation_pose(self, hand: HandObservation) -> GestureDetection | None:
        """More tolerant fist/open-palm recognizer for high-speed manipulation."""

        points = hand.landmarks
        extended = (
            self._finger_extended(hand, 8, 6),
            self._finger_extended(hand, 12, 10),
            self._finger_extended(hand, 16, 14),
            self._finger_extended(hand, 20, 18),
        )
        extended_count = sum(extended)
        wrist = points[0]
        mean_tip_radius = (
            sum(
                hypot(wrist.x - points[index].x, wrist.y - points[index].y)
                for index in (4, 8, 12, 16, 20)
            )
            / 5
        )
        pinch_distance = hypot(points[4].x - points[8].x, points[4].y - points[8].y)

        if extended_count <= 1 and mean_tip_radius <= 0.27:
            kind = GestureKind.GRAB
            metadata: dict[str, float | str] = {
                "mean_tip_radius": mean_tip_radius,
                "extended_fingers": float(extended_count),
            }
        elif extended_count >= 3 and mean_tip_radius >= 0.24:
            kind = GestureKind.OPEN_PALM
            metadata = {
                "mean_tip_radius": mean_tip_radius,
                "extended_fingers": float(extended_count),
            }
        elif pinch_distance <= 0.040 and extended_count >= 1:
            kind = GestureKind.PINCH
            metadata = {"pinch_distance": pinch_distance}
        else:
            return None

        return GestureDetection(
            kind,
            hand.confidence,
            (hand.hand_id,),
            hand.timestamp,
            metadata,
        )

    def _point_from_fingertip(self, hand: HandObservation) -> GestureDetection:
        estimate = self.advanced_pointer.estimate(hand, timestamp=hand.timestamp)
        if estimate is None:
            return super()._point_from_fingertip(hand)

        mapped_x, mapped_y = self.pointer.map(estimate.x, estimate.y)
        snapped_x, snapped_y, target_id = self.advanced_pointer.snap(
            mapped_x,
            mapped_y,
            self._interaction_targets(),
        )
        metadata: dict[str, float | str] = {
            "x": snapped_x,
            "y": snapped_y,
            "source": estimate.source,
            "pointing_confidence": estimate.confidence,
        }
        if target_id is not None:
            metadata["snap_target_id"] = target_id
        return GestureDetection(
            GestureKind.POINT,
            estimate.confidence,
            (hand.hand_id,),
            hand.timestamp,
            metadata,
        )

    async def _handle_proposal(self, proposal: SpatialActionProposal) -> None:
        await super()._handle_proposal(proposal)
        if proposal.action == SpatialAction.THROW_TO_TRASH:
            self._throw_count += 1
            self._last_terminal_action = proposal.action.value
            self._last_release_speed = float(proposal.parameters.get("speed", 0.0))
        elif proposal.action == SpatialAction.RELEASE:
            self._release_count += 1
            self._last_terminal_action = proposal.action.value
            self._last_release_speed = float(proposal.parameters.get("speed", 0.0))

    def throw_diagnostics(self) -> dict[str, float | int | str | None]:
        """Terminal manipulation diagnostics that are not overwritten by later pointer moves."""
        return {
            "throws": self._throw_count,
            "releases": self._release_count,
            "last_terminal_action": self._last_terminal_action,
            "last_release_speed": round(self._last_release_speed, 4),
            "throw_threshold": round(self.spatial.throw_velocity_threshold, 4),
        }

    async def stop(self) -> None:
        self.advanced_pointer.reset()
        await super().stop()
