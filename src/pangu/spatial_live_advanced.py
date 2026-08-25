from __future__ import annotations

from pathlib import Path

from .advanced_pointing import AdvancedPointingEstimator
from .gestures import GestureDetection, GestureKind, HandObservation
from .spatial_live import LiveSpatialDryRunRuntime


class AdvancedLiveSpatialDryRunRuntime(LiveSpatialDryRunRuntime):
    """Live dry-run runtime with ray-based pointing, adaptive filtering and target assist."""

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
