from __future__ import annotations

from pathlib import Path

from .multimodel_hand_tracking import MultiModelHandTracker
from .spatial_live_advanced import AdvancedLiveSpatialDryRunRuntime


class FusedAdvancedLiveSpatialDryRunRuntime(AdvancedLiveSpatialDryRunRuntime):
    """Advanced spatial runtime backed by MediaPipe + YOLO hand-pose fusion.

    The two landmark models consume the same camera frame through one camera owner.
    A bounded trajectory predictor bridges short occlusions/out-of-frame motion; it is
    prediction, not physical observation beyond the camera field of view.
    """

    def __init__(
        self,
        *,
        model_path: Path,
        yolo_model_path: Path,
        hud_state_path: Path,
        camera_index: int = 0,
        prediction_horizon_seconds: float = 0.45,
        **kwargs: object,
    ) -> None:
        super().__init__(
            model_path=model_path,
            hud_state_path=hud_state_path,
            camera_index=camera_index,
            **kwargs,
        )
        self.tracker = MultiModelHandTracker(
            mediapipe_model_path=model_path,
            yolo_model_path=yolo_model_path,
            camera_index=camera_index,
            max_hands=2,
            prediction_horizon_seconds=prediction_horizon_seconds,
        )

    async def start(self) -> None:
        await super().start()
        status = str(self.tracker.diagnostics().get("status", "UNKNOWN"))
        # The base runtime historically expects the single-model literal READY state.
        # Multi-model tracking exposes richer readiness states, so normalize here.
        if status in {"READY_FUSED", "DEGRADED_PRIMARY_ONLY"}:
            self._running = True

    def fusion_diagnostics(self) -> dict[str, object]:
        diagnostics = dict(self.tracker.diagnostics())
        diagnostics["true_two_model_fusion"] = diagnostics.get("secondary_status") == "READY"
        return diagnostics
