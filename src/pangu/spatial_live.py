from __future__ import annotations

import asyncio
from dataclasses import dataclass
from math import hypot
from pathlib import Path
from time import monotonic

from .browser_targets import ChromeSemanticTargetAdapter
from .events import EventBus, EventEnvelope
from .gestures import (
    GestureDetection,
    GestureKind,
    HandObservation,
    MediaPipeHandTracker,
    TemporalGestureRecognizer,
)
from .hud_bridge import HudStateBridge
from .spatial_interaction import (
    SemanticTarget,
    SpatialAction,
    SpatialActionProposal,
    SpatialInteractionController,
)


@dataclass(frozen=True)
class LiveSpatialDiagnostics:
    tracker_status: str
    browser_state: str
    browser_active: bool
    browser_targets: int
    detections: int
    proposals: int
    hover_hits: int = 0
    grab_begins: int = 0
    drag_updates: int = 0
    pointer_updates: int = 0
    last_pose: str | None = None
    last_action: str | None = None
    dry_run: bool = True


class PointerMapper:
    """Map camera-space fingertip coordinates into stable full-screen HUD coordinates."""

    def __init__(
        self,
        *,
        mirror_x: bool = True,
        x_min: float = 0.12,
        x_max: float = 0.88,
        y_min: float = 0.12,
        y_max: float = 0.88,
        smoothing: float = 0.58,
    ) -> None:
        if not 0.0 <= x_min < x_max <= 1.0:
            raise ValueError("x calibration must satisfy 0 <= min < max <= 1")
        if not 0.0 <= y_min < y_max <= 1.0:
            raise ValueError("y calibration must satisfy 0 <= min < max <= 1")
        if not 0.0 < smoothing <= 1.0:
            raise ValueError("smoothing must be in (0, 1]")
        self.mirror_x = mirror_x
        self.x_min = x_min
        self.x_max = x_max
        self.y_min = y_min
        self.y_max = y_max
        self.smoothing = smoothing
        self._x: float | None = None
        self._y: float | None = None

    @staticmethod
    def _clamp(value: float) -> float:
        return min(1.0, max(0.0, value))

    @property
    def x_span(self) -> float:
        return self.x_max - self.x_min

    @property
    def y_span(self) -> float:
        return self.y_max - self.y_min

    def map(self, x: float, y: float) -> tuple[float, float]:
        mapped_x = self._clamp((x - self.x_min) / self.x_span)
        mapped_y = self._clamp((y - self.y_min) / self.y_span)
        if self.mirror_x:
            mapped_x = 1.0 - mapped_x

        if self._x is None or self._y is None:
            self._x, self._y = mapped_x, mapped_y
        else:
            distance = hypot(mapped_x - self._x, mapped_y - self._y)
            # Large intentional movements catch up quickly; small movements remain stable.
            adaptive = min(1.0, self.smoothing + distance * 2.2)
            self._x += adaptive * (mapped_x - self._x)
            self._y += adaptive * (mapped_y - self._y)
        return self._x, self._y

    def relative_delta(self, dx: float, dy: float) -> tuple[float, float]:
        screen_dx = dx / self.x_span
        if self.mirror_x:
            screen_dx = -screen_dx
        return screen_dx, dy / self.y_span

    def reset(self) -> None:
        self._x = None
        self._y = None


class GestureStabilizer:
    """Hysteresis for manipulation poses before spatial state changes."""

    def __init__(
        self,
        *,
        grab_frames: int = 2,
        open_palm_frames: int = 2,
        pinch_frames: int = 4,
    ) -> None:
        for value in (grab_frames, open_palm_frames, pinch_frames):
            if not 1 <= value <= 12:
                raise ValueError("gesture frame thresholds must be between 1 and 12")
        self.required = {
            GestureKind.GRAB: grab_frames,
            GestureKind.OPEN_PALM: open_palm_frames,
            GestureKind.PINCH: pinch_frames,
        }
        self._last: dict[str, GestureKind] = {}
        self._streak: dict[str, int] = {}
        self._emitted: dict[str, GestureKind] = {}

    def accept(self, detection: GestureDetection) -> bool:
        required = self.required.get(detection.gesture)
        if required is None:
            return True
        hand = detection.hand_ids[0] if detection.hand_ids else "unknown"
        if self._last.get(hand) == detection.gesture:
            self._streak[hand] = self._streak.get(hand, 0) + 1
        else:
            self._last[hand] = detection.gesture
            self._streak[hand] = 1
            self._emitted.pop(hand, None)
        if self._streak[hand] < required:
            return False
        if self._emitted.get(hand) == detection.gesture:
            return False
        self._emitted[hand] = detection.gesture
        return True


class LiveSpatialDryRunRuntime:
    """Wire camera gestures, Chrome semantic targets and HUD without OS execution.

    Pointing is driven continuously from the tracked index fingertip instead of only
    from frames classified as POINT. While a target is grabbed, movement is derived
    from palm translation so closing the fist does not stop drag updates or make the
    cursor jump to the curled fingertip.
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
        browser_refresh_seconds: float = 1.2,
        poll_interval_seconds: float = 1 / 30,
    ) -> None:
        if not 0.0 <= target_padding <= 0.10:
            raise ValueError("target_padding must be between 0 and 0.10")
        self.events = EventBus(capacity=256)
        self.hud = HudStateBridge(self.events, hud_state_path, minimum_write_interval=0.04)
        self.tracker = MediaPipeHandTracker(model_path, camera_index=camera_index, max_hands=2)
        self.recognizer = TemporalGestureRecognizer()
        self.stabilizer = GestureStabilizer()
        self.browser = ChromeSemanticTargetAdapter()
        self.spatial = SpatialInteractionController()
        self.pointer = PointerMapper(
            mirror_x=mirror_x,
            x_min=pointer_x_min,
            x_max=pointer_x_max,
            y_min=pointer_y_min,
            y_max=pointer_y_max,
            smoothing=pointer_smoothing,
        )
        self.target_padding = target_padding
        self.browser_refresh_seconds = browser_refresh_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._targets: tuple[SemanticTarget, ...] = ()
        self._browser_state = "UNVERIFIED"
        self._browser_active = False
        self._last_browser_refresh = 0.0
        self._detections = 0
        self._proposals = 0
        self._hover_hits = 0
        self._grab_begins = 0
        self._drag_updates = 0
        self._pointer_updates = 0
        self._last_pose: str | None = None
        self._last_action: str | None = None
        self._running = False
        self._grab_anchor_raw: tuple[float, float] | None = None
        self._grab_pointer_origin: tuple[float, float] | None = None
        self._sticky_target_id: str | None = None
        self._sticky_until = 0.0

    @staticmethod
    def _primary_hand(hands: tuple[HandObservation, ...]) -> HandObservation | None:
        if not hands:
            return None
        return max(hands, key=lambda item: item.confidence)

    @staticmethod
    def _palm_anchor(hand: HandObservation) -> tuple[float, float]:
        indices = (0, 5, 9, 13, 17)
        x = sum(hand.landmarks[index].x for index in indices) / len(indices)
        y = sum(hand.landmarks[index].y for index in indices) / len(indices)
        return x, y

    @staticmethod
    def _finger_extended(hand: HandObservation, tip: int, pip: int) -> bool:
        return hand.landmarks[tip].y < hand.landmarks[pip].y

    def _manipulation_pose(self, hand: HandObservation) -> GestureDetection | None:
        points = hand.landmarks
        extended = (
            self._finger_extended(hand, 8, 6),
            self._finger_extended(hand, 12, 10),
            self._finger_extended(hand, 16, 14),
            self._finger_extended(hand, 20, 18),
        )
        wrist = points[0]
        mean_tip_radius = sum(
            hypot(wrist.x - points[index].x, wrist.y - points[index].y)
            for index in (4, 8, 12, 16, 20)
        ) / 5
        pinch_distance = hypot(points[4].x - points[8].x, points[4].y - points[8].y)

        # A real closed fist wins over the thumb/index proximity that otherwise makes
        # many fists look like PINCH during the closing motion.
        if sum(extended) <= 1 and mean_tip_radius <= 0.25:
            kind = GestureKind.GRAB
            metadata: dict[str, float | str] = {"mean_tip_radius": mean_tip_radius}
        elif all(extended):
            kind = GestureKind.OPEN_PALM
            metadata = {}
        elif pinch_distance <= 0.045:
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
        tip = hand.landmarks[8]
        x, y = self.pointer.map(tip.x, tip.y)
        return GestureDetection(
            GestureKind.POINT,
            hand.confidence,
            (hand.hand_id,),
            hand.timestamp,
            {"x": x, "y": y, "source": "index_fingertip"},
        )

    def _drag_from_palm(self, hand: HandObservation) -> GestureDetection | None:
        if self._grab_anchor_raw is None or self._grab_pointer_origin is None:
            return None
        current_x, current_y = self._palm_anchor(hand)
        dx = current_x - self._grab_anchor_raw[0]
        dy = current_y - self._grab_anchor_raw[1]
        screen_dx, screen_dy = self.pointer.relative_delta(dx, dy)
        x = PointerMapper._clamp(self._grab_pointer_origin[0] + screen_dx)
        y = PointerMapper._clamp(self._grab_pointer_origin[1] + screen_dy)
        return GestureDetection(
            GestureKind.POINT,
            hand.confidence,
            (hand.hand_id,),
            hand.timestamp,
            {"x": x, "y": y, "source": "grab_palm_delta"},
        )

    def _interaction_targets(self) -> tuple[SemanticTarget, ...]:
        padding = self.target_padding
        if padding <= 0.0:
            return self._targets
        expanded: list[SemanticTarget] = []
        for target in self._targets:
            x = max(0.0, target.x - padding)
            y = max(0.0, target.y - padding)
            right = min(1.0, target.x + target.width + padding)
            bottom = min(1.0, target.y + target.height + padding)
            expanded.append(
                SemanticTarget(
                    target_id=target.target_id,
                    kind=target.kind,
                    x=x,
                    y=y,
                    width=max(0.0, right - x),
                    height=max(0.0, bottom - y),
                    closable=target.closable,
                    destructive=target.destructive,
                    unsaved=target.unsaved,
                    selection_count=target.selection_count,
                )
            )
        return tuple(expanded)

    async def _refresh_browser_targets(self, *, force: bool = False) -> None:
        now = monotonic()
        if not force and now - self._last_browser_refresh < self.browser_refresh_seconds:
            return
        snapshot = self.browser.discover()
        self._browser_state = snapshot.verification_state
        self._browser_active = snapshot.window_active
        self._targets = snapshot.targets if snapshot.window_active else ()
        self._last_browser_refresh = now

    async def _publish_detection(self, detection: GestureDetection) -> None:
        await self.events.publish(
            EventEnvelope(
                "gesture.detected",
                {
                    "gesture": detection.gesture.value,
                    "confidence": detection.confidence,
                    "hand_ids": detection.hand_ids,
                    "timestamp": detection.timestamp,
                    "metadata": detection.metadata,
                },
            )
        )

    async def _publish_target(self, target: SemanticTarget) -> None:
        await self.events.publish(
            EventEnvelope(
                "spatial.target",
                {
                    "label": target.target_id.split(":")[-1][:48],
                    "target_id": target.target_id,
                    "x": target.x,
                    "y": target.y,
                    "width": target.width,
                    "height": target.height,
                    "confidence": 1.0,
                },
            )
        )

    async def _publish_proposal(self, proposal: SpatialActionProposal) -> None:
        payload = dict(proposal.parameters)
        payload.update(
            {
                "action": proposal.action.value,
                "requires_approval": proposal.requires_approval,
                "dry_run": True,
            }
        )
        await self.events.publish(EventEnvelope("spatial.proposal", payload))

    def _target_for(self, proposal: SpatialActionProposal) -> SemanticTarget | None:
        target_id = str(proposal.parameters.get("target_id", ""))
        if not target_id:
            return None
        return next((item for item in self._targets if item.target_id == target_id), None)

    def _sticky_target(self) -> SemanticTarget | None:
        if self._sticky_target_id is None or monotonic() > self._sticky_until:
            return None
        return next((item for item in self._targets if item.target_id == self._sticky_target_id), None)

    async def _handle_proposal(self, proposal: SpatialActionProposal) -> None:
        self._proposals += 1
        self._last_action = proposal.action.value
        if proposal.action == SpatialAction.HOVER_TARGET:
            self._hover_hits += 1
            target = self._target_for(proposal)
            if target is not None:
                self._sticky_target_id = target.target_id
                self._sticky_until = monotonic() + 0.9
        elif proposal.action == SpatialAction.GRAB_BEGIN:
            self._grab_begins += 1
        elif proposal.action == SpatialAction.DRAG:
            self._drag_updates += 1

        target = self._target_for(proposal)
        if target is not None:
            await self._publish_target(target)
        elif proposal.action == SpatialAction.POINTER_MOVE:
            await self.events.publish(
                EventEnvelope(
                    "spatial.target",
                    {
                        "label": "",
                        "x": 0.0,
                        "y": 0.0,
                        "width": 0.0,
                        "height": 0.0,
                        "confidence": 0.0,
                    },
                )
            )
        await self._publish_proposal(proposal)

    async def start(self) -> None:
        if self._running:
            return
        await self.events.start()
        await self.hud.start()
        self.tracker.start()
        await self._refresh_browser_targets(force=True)
        self._running = self.tracker.diagnostics().get("status") == "READY"
        await self.events.publish(
            EventEnvelope(
                "awareness.notice",
                {
                    "subject": "SPATIAL DRY RUN",
                    "message": "Continuous finger pointer + fist drag; execution disabled",
                    "importance": 0.8,
                },
            )
        )

    async def stop(self) -> None:
        self._running = False
        self.tracker.stop()
        self.pointer.reset()
        await self.hud.stop()
        await self.events.stop()

    async def poll_once(self) -> tuple[SpatialActionProposal, ...]:
        await self._refresh_browser_targets()
        proposals: list[SpatialActionProposal] = []
        hands = self.tracker.read()
        primary = self._primary_hand(hands)
        interaction_targets = self._interaction_targets()

        if primary is None:
            return ()

        pose = self._manipulation_pose(primary)
        if pose is not None:
            self._last_pose = pose.gesture.value

        # Continuous pointer update. On the exact fist-closing frame keep the previous
        # pointer position so the curled index fingertip cannot jump away from a target.
        if self.spatial.state.grabbed:
            pointer_detection = self._drag_from_palm(primary)
        elif pose is not None and pose.gesture == GestureKind.GRAB:
            pointer_detection = None
        else:
            pointer_detection = self._point_from_fingertip(primary)

        if pointer_detection is not None:
            self._detections += 1
            self._pointer_updates += 1
            await self._publish_detection(pointer_detection)
            proposal = self.spatial.propose(pointer_detection, interaction_targets)
            if proposal is not None:
                proposals.append(proposal)
                await self._handle_proposal(proposal)

        if pose is not None:
            self._detections += 1
            await self._publish_detection(pose)
            if self.stabilizer.accept(pose):
                # The sticky target gives the user a short grace period to close the fist
                # after the target glow appears. Pointer position itself is not changed.
                sticky = self._sticky_target()
                targets_for_pose = interaction_targets
                if pose.gesture == GestureKind.GRAB and sticky is not None:
                    targets_for_pose = tuple(
                        target
                        for target in interaction_targets
                        if target.target_id == sticky.target_id
                    ) or interaction_targets

                proposal = self.spatial.propose(pose, targets_for_pose)
                if proposal is not None:
                    proposals.append(proposal)
                    await self._handle_proposal(proposal)
                    if proposal.action == SpatialAction.GRAB_BEGIN:
                        self._grab_anchor_raw = self._palm_anchor(primary)
                        pointer_x = self.spatial.state.pointer_x or 0.0
                        pointer_y = self.spatial.state.pointer_y or 0.0
                        self._grab_pointer_origin = (pointer_x, pointer_y)
                    elif proposal.action in {SpatialAction.RELEASE, SpatialAction.THROW_TO_TRASH}:
                        self._grab_anchor_raw = None
                        self._grab_pointer_origin = None

        # Preserve temporal two-hand/swipe recognition for later spatial commands, but
        # do not duplicate static POINT/PINCH/GRAB/OPEN_PALM decisions handled above.
        for temporal in self.recognizer.recognize(hands):
            if temporal.gesture in {
                GestureKind.POINT,
                GestureKind.PINCH,
                GestureKind.GRAB,
                GestureKind.OPEN_PALM,
            }:
                continue
            self._detections += 1
            await self._publish_detection(temporal)
            proposal = self.spatial.propose(temporal, interaction_targets)
            if proposal is not None:
                proposals.append(proposal)
                await self._handle_proposal(proposal)

        return tuple(proposals)

    async def run(self, duration_seconds: float | None = None) -> None:
        started = monotonic()
        while self._running:
            if duration_seconds is not None and monotonic() - started >= duration_seconds:
                break
            await self.poll_once()
            await asyncio.sleep(self.poll_interval_seconds)

    def diagnostics(self) -> LiveSpatialDiagnostics:
        return LiveSpatialDiagnostics(
            tracker_status=str(self.tracker.diagnostics().get("status", "UNKNOWN")),
            browser_state=self._browser_state,
            browser_active=self._browser_active,
            browser_targets=len(self._targets),
            detections=self._detections,
            proposals=self._proposals,
            hover_hits=self._hover_hits,
            grab_begins=self._grab_begins,
            drag_updates=self._drag_updates,
            pointer_updates=self._pointer_updates,
            last_pose=self._last_pose,
            last_action=self._last_action,
            dry_run=True,
        )
