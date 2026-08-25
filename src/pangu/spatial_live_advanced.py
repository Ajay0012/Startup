from __future__ import annotations

from math import hypot
from pathlib import Path

from .advanced_pointing import AdvancedPointingEstimator
from .gestures import GestureDetection, GestureKind, HandObservation
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
        self.advanced_pointer = AdvancedPointingEstimator(
            ray_gain=ray_gain,
            snap_radius=snap_radius,
            snap_strength=snap_strength,
        )
        # A fast throw often exposes the palm for only a few video frames. Accept
        # release immediately while keeping GRAB and PINCH guarded against flicker.
        self.stabilizer = GestureStabilizer(
            grab_frames=2,
            open_palm_frames=1,
            pinch_frames=5,
        )

    def _manipulation_pose(self, hand: HandObservation) -> GestureDetection | None:
        """More tolerant fist/open-palm recognizer for high-speed manipulation.

        MediaPipe landmarks become noisy while the hand is moving quickly. Requiring
        all four fingers to be perfectly extended can miss the release frame, so the
        advanced runtime treats three-or-more clearly extended fingers as OPEN_PALM.
        A compact hand with at most one extended finger remains GRAB.
        """

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

    async def stop(self) -> None:
        self.advanced_pointer.reset()
        await super().stop()
