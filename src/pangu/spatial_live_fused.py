from __future__ import annotations

from pathlib import Path

from .directional_intent import DirectionalIntentAssist
from .gestures import GestureDetection, GestureKind, HandObservation
from .multimodel_hand_tracking import MultiModelHandTracker
from .spatial_live_advanced import AdvancedLiveSpatialDryRunRuntime


class FusedAdvancedLiveSpatialDryRunRuntime(AdvancedLiveSpatialDryRunRuntime):
    """Advanced spatial runtime backed by MediaPipe + YOLO hand-pose fusion.

    The two landmark models consume the same camera frame through one camera owner.
    A bounded trajectory predictor bridges short occlusions/out-of-frame motion; it is
    prediction, not physical observation beyond the camera field of view. Direction,
    movement length and speed are also used to bias the pointer toward likely semantic
    targets without generating clicks or destructive actions.
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
        self.intent_assist = DirectionalIntentAssist()
        self._intent_target_id: str | None = None
        self._intent_speed = 0.0

    def _point_from_fingertip(self, hand: HandObservation) -> GestureDetection:
        detection = super()._point_from_fingertip(hand)
        metadata = dict(detection.metadata)
        x = float(metadata.get("x", 0.0))
        y = float(metadata.get("y", 0.0))

        # Existing precision lock wins. Directional intent is used when the user is
        # moving toward a target but has not yet entered the near-target lock radius.
        if metadata.get("snap_target_id"):
            self._intent_target_id = str(metadata["snap_target_id"])
            self._intent_speed = 0.0
            return detection

        intent = self.intent_assist.apply(
            x,
            y,
            hand.timestamp,
            self._interaction_targets(),
        )
        self._intent_target_id = intent.target_id
        self._intent_speed = intent.speed
        metadata["x"] = intent.x
        metadata["y"] = intent.y
        metadata["motion_speed"] = intent.speed
        metadata["projected_x"] = intent.projected_x
        metadata["projected_y"] = intent.projected_y
        metadata["source"] = "multimodel-directional-intent"
        if intent.target_id is not None:
            metadata["intent_target_id"] = intent.target_id
        return GestureDetection(
            GestureKind.POINT,
            detection.confidence,
            detection.hand_ids,
            detection.timestamp,
            metadata,
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
        diagnostics["intent_target_id"] = self._intent_target_id
        diagnostics["intent_speed"] = round(self._intent_speed, 4)
        return diagnostics

    async def stop(self) -> None:
        self.intent_assist.reset()
        await super().stop()
