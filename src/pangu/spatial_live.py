from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from .browser_targets import ChromeSemanticTargetAdapter
from .events import EventBus, EventEnvelope
from .gestures import (
    GestureDetection,
    GestureKind,
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
    last_action: str | None = None
    dry_run: bool = True


class PointerMapper:
    """Map camera-space fingertip coordinates into stable full-screen HUD coordinates.

    The camera rarely uses its extreme image edges during natural pointing, so the
    configurable active region is expanded to the full desktop. A small EMA removes
    landmark jitter without turning the cursor into a delayed mouse substitute.
    """

    def __init__(
        self,
        *,
        mirror_x: bool = True,
        x_min: float = 0.12,
        x_max: float = 0.88,
        y_min: float = 0.12,
        y_max: float = 0.88,
        smoothing: float = 0.38,
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

    def map(self, x: float, y: float) -> tuple[float, float]:
        mapped_x = self._clamp((x - self.x_min) / (self.x_max - self.x_min))
        mapped_y = self._clamp((y - self.y_min) / (self.y_max - self.y_min))
        if self.mirror_x:
            mapped_x = 1.0 - mapped_x

        if self._x is None or self._y is None:
            self._x, self._y = mapped_x, mapped_y
        else:
            alpha = self.smoothing
            self._x += alpha * (mapped_x - self._x)
            self._y += alpha * (mapped_y - self._y)
        return self._x, self._y

    def reset(self) -> None:
        self._x = None
        self._y = None


class GestureStabilizer:
    """Small hysteresis layer for discrete gestures before spatial state changes.

    POINT and temporal gestures pass through immediately. GRAB, OPEN_PALM and PINCH
    require a short consecutive streak so single-frame pose flicker cannot trigger a
    state transition. This runtime is dry-run only; no OS action is performed.
    """

    _discrete = {GestureKind.GRAB, GestureKind.OPEN_PALM, GestureKind.PINCH}

    def __init__(self, required_frames: int = 2) -> None:
        if not 1 <= required_frames <= 12:
            raise ValueError("required_frames must be between 1 and 12")
        self.required_frames = required_frames
        self._last: dict[str, GestureKind] = {}
        self._streak: dict[str, int] = {}
        self._emitted: dict[str, GestureKind] = {}

    def accept(self, detection: GestureDetection) -> bool:
        if detection.gesture not in self._discrete:
            return True
        hand = detection.hand_ids[0] if detection.hand_ids else "unknown"
        if self._last.get(hand) == detection.gesture:
            self._streak[hand] = self._streak.get(hand, 0) + 1
        else:
            self._last[hand] = detection.gesture
            self._streak[hand] = 1
            self._emitted.pop(hand, None)
        if self._streak[hand] < self.required_frames:
            return False
        if self._emitted.get(hand) == detection.gesture:
            return False
        self._emitted[hand] = detection.gesture
        return True


class LiveSpatialDryRunRuntime:
    """Wire camera gestures, Chrome semantic targets and HUD without OS execution."""

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
        pointer_smoothing: float = 0.38,
        target_padding: float = 0.018,
        browser_refresh_seconds: float = 1.5,
        poll_interval_seconds: float = 1 / 30,
    ) -> None:
        if not 0.0 <= target_padding <= 0.08:
            raise ValueError("target_padding must be between 0 and 0.08")
        self.events = EventBus(capacity=256)
        self.hud = HudStateBridge(self.events, hud_state_path, minimum_write_interval=0.04)
        self.tracker = MediaPipeHandTracker(model_path, camera_index=camera_index, max_hands=2)
        self.recognizer = TemporalGestureRecognizer()
        self.stabilizer = GestureStabilizer(required_frames=2)
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
        self._last_action: str | None = None
        self._running = False

    def _screen_detection(self, detection: GestureDetection) -> GestureDetection:
        if detection.gesture != GestureKind.POINT:
            return detection
        metadata = dict(detection.metadata)
        x = float(metadata.get("x", 0.0))
        y = float(metadata.get("y", 0.0))
        mapped_x, mapped_y = self.pointer.map(x, y)
        metadata["x"] = mapped_x
        metadata["y"] = mapped_y
        return GestureDetection(
            detection.gesture,
            detection.confidence,
            detection.hand_ids,
            detection.timestamp,
            metadata,
        )

    def _interaction_targets(self) -> tuple[SemanticTarget, ...]:
        """Use a modest dry-run acquisition halo while preserving original IDs.

        The visual target remains exact. Only hit-testing is slightly expanded so a
        camera fingertip does not need mouse-pixel precision to acquire a tab.
        """
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
        raw = proposal.parameters.get("target_id", "")
        target_id = str(raw)
        if not target_id:
            return None
        return next((item for item in self._targets if item.target_id == target_id), None)

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
                    "message": "Camera gestures + Chrome targets; execution disabled",
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
        detections = self.recognizer.recognize(self.tracker.read())
        interaction_targets = self._interaction_targets()
        for raw in detections:
            detection = self._screen_detection(raw)
            self._detections += 1
            await self._publish_detection(detection)
            if not self.stabilizer.accept(detection):
                continue
            proposal = self.spatial.propose(detection, interaction_targets)
            if proposal is None:
                continue
            self._proposals += 1
            self._last_action = proposal.action.value
            if proposal.action == SpatialAction.HOVER_TARGET:
                self._hover_hits += 1
            elif proposal.action == SpatialAction.GRAB_BEGIN:
                self._grab_begins += 1
            proposals.append(proposal)
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
            last_action=self._last_action,
            dry_run=True,
        )
