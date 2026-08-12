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


@dataclass
class HudRuntimeState:
    mode: str = "listening"
    status: str = "AMBIENT • LISTENING"
    message: str | None = None
    audio_level: float = 0.08
    cards: list[HudCard] = field(default_factory=list)
    target: HudTarget | None = None
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
        "gesture.recognized",
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
        elif topic == "gesture.recognized":
            label = self._safe_text(payload.get("gesture", "gesture"), 32)
            x = float(payload.get("x", 0.0))
            y = float(payload.get("y", 0.0))
            confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.0))))
            self.state.target = HudTarget(label, x, y, 0.08, 0.08, confidence)
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
