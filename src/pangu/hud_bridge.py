from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from .events import EventBus, EventEnvelope


@dataclass(frozen=True)
class HudCard:
    title: str
    value: str
    detail: str | None = None


@dataclass(frozen=True)
class HudTarget:
    label: str
    x: float
    y: float
    width: float
    height: float
    confidence: float = 1.0


@dataclass(frozen=True)
class HudPoint:
    x: float
    y: float


@dataclass(frozen=True)
class HudZone:
    label: str
    x: float
    y: float
    width: float
    height: float
    active: bool = False


@dataclass
class HudSpatialState:
    pointer: HudPoint | None = None
    trail: list[HudPoint] = field(default_factory=list)
    gesture: str | None = None
    grabbed: bool = False
    grabbed_target_id: str | None = None
    interaction: str | None = None
    confirmation_required: bool = False
    throw_speed: float = 0.0
    trash_zone: HudZone = field(
        default_factory=lambda: HudZone("TRASH", 0.82, 0.72, 0.16, 0.22, False)
    )


@dataclass
class HudRuntimeState:
    mode: str = "listening"
    status: str = "AMBIENT • LISTENING"
    message: str | None = None
    audio_level: float = 0.08
    cards: list[HudCard] = field(default_factory=list)
    target: HudTarget | None = None
    spatial: HudSpatialState = field(default_factory=HudSpatialState)
    updated_at: str = ""


class HudStateBridge:
    """Publish bounded, non-sensitive runtime state to the native HUD state file."""

    _subscriptions = (
        "voice.wake.detected",
        "voice.command.capture.started",
        "voice.transcription.completed",
        "voice.response.started",
        "voice.response.completed",
        "voice.turn.completed",
        "voice.transcription.failed",
        "awareness.notice",
        "mission.started",
        "mission.completed",
        "mission.failed",
        "world.delta",
        "gesture.detected",
        "gesture.recognized",
        "spatial.target",
        "spatial.proposal",
        "spatial.confirmation",
    )

    def __init__(
        self, events: EventBus, path: Path, *, minimum_write_interval: float = 0.08
    ) -> None:
        self.events = events
        self.path = path
        self.minimum_write_interval = minimum_write_interval
        self.state = HudRuntimeState()
        self._running = False
        self._last_write = 0.0
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for topic in self._subscriptions:
            self.events.subscribe(topic, self._on_event)
        await self._write(force=True)

    async def stop(self) -> None:
        if not self._running:
            return
        for topic in self._subscriptions:
            self.events.unsubscribe(topic, self._on_event)
        self._running = False

    @staticmethod
    def _safe_text(value: object, limit: int = 180) -> str:
        text = " ".join(str(value).split())
        return text[:limit]

    @staticmethod
    def _clamp(value: object, fallback: float = 0.0) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return fallback

    def _set_pointer(self, x: object, y: object) -> None:
        point = HudPoint(self._clamp(x), self._clamp(y))
        self.state.spatial.pointer = point
        self.state.spatial.trail.append(point)
        del self.state.spatial.trail[:-18]

    async def _on_event(self, event: EventEnvelope) -> None:
        if not self._running:
            return
        topic = event.topic
        payload = event.payload
        if topic == "voice.wake.detected":
            self.state.mode = "listening"
            self.state.status = "AWAKE • LISTENING"
            self.state.message = "I'm listening."
        elif topic == "voice.command.capture.started":
            self.state.mode = "listening"
            self.state.status = "COMMAND • LISTENING"
            self.state.message = None
        elif topic == "voice.transcription.completed":
            self.state.mode = "thinking"
            self.state.status = "THINKING"
            self.state.message = self._safe_text(payload.get("text", ""))
        elif topic == "voice.response.started":
            self.state.mode = "speaking"
            self.state.status = "SPEAKING"
        elif topic in {"voice.response.completed", "voice.turn.completed"}:
            self.state.mode = "listening"
            self.state.status = "AMBIENT • LISTENING"
            self.state.message = None
        elif topic == "voice.transcription.failed":
            self.state.mode = "error"
            self.state.status = "VOICE • DEGRADED"
            self.state.message = self._safe_text(payload.get("normalized_error", "voice error"))
        elif topic == "awareness.notice":
            self.state.cards.insert(
                0,
                HudCard(
                    self._safe_text(payload.get("subject", "NOTICE"), 42),
                    self._safe_text(payload.get("message", ""), 80),
                    f"importance {float(payload.get('importance', 0.0)):.2f}",
                ),
            )
            del self.state.cards[6:]
        elif topic == "mission.started":
            self.state.cards.insert(
                0,
                HudCard("MISSION", "RUNNING", self._safe_text(payload.get("goal", ""), 72)),
            )
            del self.state.cards[6:]
        elif topic in {"mission.completed", "mission.failed"}:
            state = "COMPLETED" if topic.endswith("completed") else "FAILED"
            self.state.cards.insert(
                0,
                HudCard("MISSION", state, self._safe_text(payload.get("mission_id", ""), 48)),
            )
            del self.state.cards[6:]
        elif topic == "world.delta" and bool(payload.get("changed", False)):
            entity = self._safe_text(payload.get("entity", "world"), 32)
            attribute = self._safe_text(payload.get("attribute", "state"), 32)
            self.state.cards.insert(
                0,
                HudCard(
                    entity.upper(),
                    attribute,
                    self._safe_text(payload.get("message", "changed"), 72),
                ),
            )
            del self.state.cards[6:]
        elif topic in {"gesture.detected", "gesture.recognized"}:
            label = self._safe_text(payload.get("gesture", "gesture"), 32)
            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            x = metadata.get("x", payload.get("x"))
            y = metadata.get("y", payload.get("y"))
            if x is not None and y is not None:
                self._set_pointer(x, y)
            self.state.spatial.gesture = label
        elif topic == "spatial.target":
            confidence = self._clamp(payload.get("confidence", 1.0), 1.0)
            self.state.target = HudTarget(
                self._safe_text(payload.get("label", "TARGET"), 48),
                self._clamp(payload.get("x")),
                self._clamp(payload.get("y")),
                self._clamp(payload.get("width"), 0.08),
                self._clamp(payload.get("height"), 0.08),
                confidence,
            )
        elif topic == "spatial.proposal":
            action = self._safe_text(payload.get("action", ""), 40)
            self.state.spatial.interaction = action or None
            self.state.spatial.grabbed = bool(payload.get("grabbed", action in {"GRAB_BEGIN", "DRAG"}))
            target_id = self._safe_text(payload.get("target_id", ""), 120)
            self.state.spatial.grabbed_target_id = target_id or None
            try:
                self.state.spatial.throw_speed = max(0.0, float(payload.get("speed", 0.0)))
            except (TypeError, ValueError):
                self.state.spatial.throw_speed = 0.0
            self.state.spatial.confirmation_required = bool(
                payload.get("requires_approval", False)
            )
            self.state.spatial.trash_zone = HudZone(
                "TRASH",
                0.82,
                0.72,
                0.16,
                0.22,
                self.state.spatial.grabbed or action == "THROW_TO_TRASH",
            )
            if action in {"RELEASE", "THROW_TO_TRASH"}:
                self.state.spatial.grabbed = False
                self.state.spatial.grabbed_target_id = None
        elif topic == "spatial.confirmation":
            self.state.spatial.confirmation_required = bool(payload.get("required", False))
            if self.state.spatial.confirmation_required:
                self.state.status = "CONFIRMATION REQUIRED"
        await self._write()

    async def set_audio_level(self, value: float) -> None:
        self.state.audio_level = max(0.0, min(1.0, value))
        await self._write()

    async def _write(self, *, force: bool = False) -> None:
        now = monotonic()
        if not force and now - self._last_write < self.minimum_write_interval:
            return
        async with self._write_lock:
            now = monotonic()
            if not force and now - self._last_write < self.minimum_write_interval:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.state.updated_at = (
                __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()
            )
            spatial = self.state.spatial
            payload = {
                "Mode": self.state.mode,
                "Status": self.state.status,
                "Message": self.state.message,
                "AudioLevel": self.state.audio_level,
                "Cards": [
                    {"Title": card.title, "Value": card.value, "Detail": card.detail}
                    for card in self.state.cards[:6]
                ],
                "Target": None
                if self.state.target is None
                else {
                    "Label": self.state.target.label,
                    "X": self.state.target.x,
                    "Y": self.state.target.y,
                    "Width": self.state.target.width,
                    "Height": self.state.target.height,
                    "Confidence": self.state.target.confidence,
                },
                "Spatial": {
                    "Pointer": None
                    if spatial.pointer is None
                    else {"X": spatial.pointer.x, "Y": spatial.pointer.y},
                    "Trail": [{"X": point.x, "Y": point.y} for point in spatial.trail[-18:]],
                    "Gesture": spatial.gesture,
                    "Grabbed": spatial.grabbed,
                    "GrabbedTargetId": spatial.grabbed_target_id,
                    "Interaction": spatial.interaction,
                    "ConfirmationRequired": spatial.confirmation_required,
                    "ThrowSpeed": spatial.throw_speed,
                    "TrashZone": {
                        "Label": spatial.trash_zone.label,
                        "X": spatial.trash_zone.x,
                        "Y": spatial.trash_zone.y,
                        "Width": spatial.trash_zone.width,
                        "Height": spatial.trash_zone.height,
                        "Active": spatial.trash_zone.active,
                    },
                },
                "UpdatedAt": self.state.updated_at,
            }
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            fd, temporary = tempfile.mkstemp(
                prefix="pangu-hud-", suffix=".json", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._last_write = now
