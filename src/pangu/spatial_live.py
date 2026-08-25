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
    dry_run: bool = True


class GestureStabilizer:
    """Small hysteresis layer for discrete gestures before spatial state changes.

    POINT and temporal gestures pass through immediately. GRAB, OPEN_PALM and PINCH
    require a short consecutive streak so single-frame pose flicker cannot trigger a
    state transition. This runtime is dry-run only; no OS action is performed.
    """

    _discrete = {GestureKind.GRAB, GestureKind.OPEN_PALM, GestureKind.PINCH}

    def __init__(self, required_frames: int = 3) -> None:
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
        browser_refresh_seconds: float = 0.45,
        poll_interval_seconds: float = 1 / 30,
    ) -> None:
        self.events = EventBus(capacity=256)
        self.hud = HudStateBridge(self.events, hud_state_path, minimum_write_interval=0.04)
        self.tracker = MediaPipeHandTracker(model_path, camera_index=camera_index, max_hands=2)
        self.recognizer = TemporalGestureRecognizer()
        self.stabilizer = GestureStabilizer()
        self.browser = ChromeSemanticTargetAdapter()
        self.spatial = SpatialInteractionController()
        self.mirror_x = mirror_x
        self.browser_refresh_seconds = browser_refresh_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._targets: tuple[SemanticTarget, ...] = ()
        self._browser_state = "UNVERIFIED"
        self._browser_active = False
        self._last_browser_refresh = 0.0
        self._detections = 0
        self._proposals = 0
        self._running = False

    def _screen_detection(self, detection: GestureDetection) -> GestureDetection:
        if detection.gesture != GestureKind.POINT:
            return detection
        metadata = dict(detection.metadata)
        x = float(metadata.get("x", 0.0))
        y = float(metadata.get("y", 0.0))
        metadata["x"] = 1.0 - x if self.mirror_x else x
        metadata["y"] = y
        return GestureDetection(
            detection.gesture,
            detection.confidence,
            detection.hand_ids,
            detection.timestamp,
            metadata,
        )

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
        await self.hud.stop()
        await self.events.stop()

    async def poll_once(self) -> tuple[SpatialActionProposal, ...]:
        await self._refresh_browser_targets()
        proposals: list[SpatialActionProposal] = []
        detections = self.recognizer.recognize(self.tracker.read())
        for raw in detections:
            detection = self._screen_detection(raw)
            self._detections += 1
            await self._publish_detection(detection)
            if not self.stabilizer.accept(detection):
                continue
            proposal = self.spatial.propose(detection, self._targets)
            if proposal is None:
                continue
            self._proposals += 1
            proposals.append(proposal)
            target = self._target_for(proposal)
            if target is not None:
                await self._publish_target(target)
            elif proposal.action == SpatialAction.POINTER_MOVE:
                # No target under the pointer: clear target by publishing a zero-confidence marker.
                await self.events.publish(
                    EventEnvelope(
                        "spatial.target",
                        {"label": "", "x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0, "confidence": 0.0},
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
            dry_run=True,
        )
