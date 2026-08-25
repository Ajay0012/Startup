from __future__ import annotations

from collections import deque
from math import hypot
from pathlib import Path
from time import monotonic

from .advanced_pointing import AdvancedPointingEstimator
from .gestures import GestureDetection, GestureKind, HandObservation
from .precision_motion import PrecisionDragController, PrecisionTargetLock
from .precision_targets import PrecisionSemanticTargetAdapter
from .spatial_interaction import SpatialAction, SpatialActionProposal
from .spatial_live import GestureStabilizer, LiveSpatialDryRunRuntime


class AdvancedLiveSpatialDryRunRuntime(LiveSpatialDryRunRuntime):
    """Live dry-run runtime with ray pointing and precision semantic interaction.

    A single RGB webcam cannot reproduce Vision Pro's eye tracking or depth-sensor
    accuracy. This runtime instead closes the software gap with semantic UI targets,
    target-lock hysteresis, velocity-aware assistance and clutch-like precision drag.
    """

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
        self.precision_targets = PrecisionSemanticTargetAdapter()
        self.precision_lock = PrecisionTargetLock()
        self.precision_drag = PrecisionDragController()
        self.spatial.throw_velocity_threshold = throw_velocity_threshold
        self.stabilizer = GestureStabilizer(
            grab_frames=2,
            open_palm_frames=1,
            pinch_frames=5,
        )
        self._throw_count = 0
        self._release_count = 0
        self._last_terminal_action: str | None = None
        self._last_release_speed = 0.0
        self._semantic_target_count = 0
        self._precision_locked_target_id: str | None = None
        self._precision_mode = False
        self._drag_velocity_history: deque[tuple[float, float, float]] = deque(maxlen=48)

    async def _refresh_browser_targets(self, *, force: bool = False) -> None:
        now = monotonic()
        if not force and now - self._last_browser_refresh < self.browser_refresh_seconds:
            return

        browser = self.browser.discover()
        semantic = self.precision_targets.discover()
        self._browser_state = browser.verification_state
        self._browser_active = browser.window_active
        merged = list(browser.targets if browser.window_active else ())
        seen = {target.target_id for target in merged}
        for target in semantic.targets:
            if target.target_id not in seen:
                merged.append(target)
                seen.add(target.target_id)
        self._targets = tuple(merged)
        self._semantic_target_count = len(semantic.targets)
        self._last_browser_refresh = now

    def _manipulation_pose(self, hand: HandObservation) -> GestureDetection | None:
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
        precise = self.precision_lock.apply(
            mapped_x,
            mapped_y,
            hand.timestamp,
            self._interaction_targets(),
        )
        self._precision_locked_target_id = precise.locked_target_id
        self._precision_mode = precise.precision_mode
        metadata: dict[str, float | str] = {
            "x": precise.x,
            "y": precise.y,
            "source": "semantic-ray-precision",
            "pointing_confidence": estimate.confidence,
            "precision_mode": "true" if precise.precision_mode else "false",
        }
        if precise.locked_target_id is not None:
            metadata["snap_target_id"] = precise.locked_target_id
        return GestureDetection(
            GestureKind.POINT,
            estimate.confidence,
            (hand.hand_id,),
            hand.timestamp,
            metadata,
        )

    def _drag_from_palm(self, hand: HandObservation) -> GestureDetection | None:
        palm_x, palm_y = self._palm_anchor(hand)
        precise = self.precision_drag.update(
            palm_x,
            palm_y,
            x_span=self.pointer.x_span,
            y_span=self.pointer.y_span,
            mirror_x=self.pointer.mirror_x,
        )
        if precise is None:
            pointer_x = self.spatial.state.pointer_x or 0.0
            pointer_y = self.spatial.state.pointer_y or 0.0
            self.precision_drag.begin(palm_x, palm_y, pointer_x, pointer_y)
            precise = (pointer_x, pointer_y)

        x, y = precise
        detection = GestureDetection(
            GestureKind.POINT,
            hand.confidence,
            (hand.hand_id,),
            hand.timestamp,
            {"x": x, "y": y, "source": "precision_incremental_palm"},
        )
        self._drag_velocity_history.append((detection.timestamp, x, y))
        cutoff = detection.timestamp - 0.9
        while self._drag_velocity_history and self._drag_velocity_history[0][0] < cutoff:
            self._drag_velocity_history.popleft()
        return detection

    def _robust_throw_speed(self) -> float:
        samples = tuple(self._drag_velocity_history)
        if len(samples) < 2:
            return 0.0
        best = 0.0
        for i, start in enumerate(samples[:-1]):
            for end in samples[i + 1 :]:
                elapsed = end[0] - start[0]
                if elapsed < 0.06 or elapsed > 0.55:
                    continue
                distance = hypot(end[1] - start[1], end[2] - start[2])
                if distance < 0.025:
                    continue
                best = max(best, distance / elapsed)
        return best

    def _upgrade_release_to_throw(self, proposal: SpatialActionProposal) -> SpatialActionProposal:
        if proposal.action != SpatialAction.RELEASE:
            return proposal
        target_id = str(proposal.parameters.get("target_id", ""))
        target = next((item for item in self._targets if item.target_id == target_id), None)
        robust_speed = self._robust_throw_speed()
        if (
            target is None
            or not target.closable
            or robust_speed < self.spatial.throw_velocity_threshold
        ):
            return proposal
        approval = bool(target.destructive or target.unsaved or target.selection_count > 1)
        return SpatialActionProposal(
            SpatialAction.THROW_TO_TRASH,
            proposal.hand_ids,
            proposal.confidence,
            {
                "target_id": target.target_id,
                "target_kind": target.kind,
                "selection_count": target.selection_count,
                "unsaved": target.unsaved,
                "speed": robust_speed,
                "throw_anywhere": True,
                "velocity_source": "precision_palm_history",
            },
            requires_target_resolution=True,
            requires_approval=approval,
        )

    async def _handle_proposal(self, proposal: SpatialActionProposal) -> None:
        proposal = self._upgrade_release_to_throw(proposal)
        await super()._handle_proposal(proposal)
        if proposal.action == SpatialAction.THROW_TO_TRASH:
            self._throw_count += 1
            self._last_terminal_action = proposal.action.value
            self._last_release_speed = float(proposal.parameters.get("speed", 0.0))
            self._drag_velocity_history.clear()
            self.precision_drag.reset()
        elif proposal.action == SpatialAction.RELEASE:
            self._release_count += 1
            self._last_terminal_action = proposal.action.value
            self._last_release_speed = max(
                float(proposal.parameters.get("speed", 0.0)), self._robust_throw_speed()
            )
            self._drag_velocity_history.clear()
            self.precision_drag.reset()
        elif proposal.action == SpatialAction.GRAB_BEGIN:
            self._drag_velocity_history.clear()
            self.precision_drag.reset()

    def throw_diagnostics(self) -> dict[str, float | int | str | None]:
        return {
            "throws": self._throw_count,
            "releases": self._release_count,
            "last_terminal_action": self._last_terminal_action,
            "last_release_speed": round(self._last_release_speed, 4),
            "throw_threshold": round(self.spatial.throw_velocity_threshold, 4),
        }

    def precision_diagnostics(self) -> dict[str, int | str | bool | None]:
        return {
            "semantic_targets": self._semantic_target_count,
            "locked_target_id": self._precision_locked_target_id,
            "precision_mode": self._precision_mode,
        }

    async def stop(self) -> None:
        self.advanced_pointer.reset()
        self.precision_lock.reset()
        self.precision_drag.reset()
        self._drag_velocity_history.clear()
        await super().stop()
