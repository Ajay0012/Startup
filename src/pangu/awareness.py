from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from .events import EventBus, EventEnvelope, EventPriority
from .memory import MemoryKind, PersistentMemoryRuntime


@dataclass(frozen=True)
class ProactivePolicy:
    minimum_importance: float = 0.75
    cooldown_seconds: int = 300
    maximum_notices_per_hour: int = 6

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_importance <= 1:
            raise ValueError("minimum_importance must be between 0 and 1")
        if self.cooldown_seconds < 1 or not 1 <= self.maximum_notices_per_hour <= 60:
            raise ValueError("invalid proactive policy")


class ProactiveAwarenessRuntime:
    """Bounded attention manager for important runtime/world deltas.

    It may emit a notice; it never executes the underlying consequential action.
    Owner dismissals are remembered so repeated suggestions are suppressed.
    """

    def __init__(
        self,
        events: EventBus,
        memory: PersistentMemoryRuntime,
        policy: ProactivePolicy | None = None,
    ) -> None:
        self.events = events
        self.memory = memory
        self.policy = policy or ProactivePolicy()
        self._running = False
        self._last_notice: dict[str, datetime] = {}
        self._hourly: deque[datetime] = deque()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.events.subscribe("world.delta", self._on_delta)
        self.events.subscribe("mission.failed", self._on_mission_failure)
        self.events.subscribe("awareness.dismissed", self._on_dismissed)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self.events.unsubscribe("world.delta", self._on_delta)
        self.events.unsubscribe("mission.failed", self._on_mission_failure)
        self.events.unsubscribe("awareness.dismissed", self._on_dismissed)
        self._hourly.clear()

    @staticmethod
    def _key(subject: str) -> str:
        return sha256(subject.casefold().encode("utf-8")).hexdigest()[:24]

    def _dismissed(self, key: str) -> bool:
        return bool(
            self.memory.recall(
                f"proactive suppression {key}",
                kinds=(MemoryKind.PROCEDURAL,),
                limit=1,
            )
        )

    def _admit(self, key: str, importance: float) -> bool:
        if importance < self.policy.minimum_importance or self._dismissed(key):
            return False
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=1)
        while self._hourly and self._hourly[0] < cutoff:
            self._hourly.popleft()
        if len(self._hourly) >= self.policy.maximum_notices_per_hour:
            return False
        previous = self._last_notice.get(key)
        if previous is not None and (now - previous).total_seconds() < self.policy.cooldown_seconds:
            return False
        self._last_notice[key] = now
        self._hourly.append(now)
        return True

    async def _emit_notice(
        self, subject: str, message: str, importance: float, evidence: dict[str, object]
    ) -> None:
        key = self._key(subject)
        if not self._admit(key, importance):
            return
        await self.events.publish(
            EventEnvelope(
                "awareness.notice",
                {
                    "notice_key": key,
                    "subject": subject,
                    "message": message,
                    "importance": importance,
                    "evidence": evidence,
                    "action_authorized": False,
                },
                EventPriority.LOW,
            )
        )

    async def _on_delta(self, event: EventEnvelope) -> None:
        if not self._running or not bool(event.payload.get("changed", False)):
            return
        entity = str(event.payload.get("entity", "world"))
        attribute = str(event.payload.get("attribute", "state"))
        importance = float(event.payload.get("importance", 0.5))
        await self._emit_notice(
            f"{entity}:{attribute}",
            str(event.payload.get("message", f"{entity} {attribute} changed.")),
            importance,
            {"event_id": event.event_id, "source": event.payload.get("source", "runtime")},
        )

    async def _on_mission_failure(self, event: EventEnvelope) -> None:
        await self._emit_notice(
            f"mission:{event.payload.get('mission_id', 'unknown')}:failed",
            "A PANGU mission stopped because a task failed.",
            0.9,
            {"event_id": event.event_id, "mission_id": event.payload.get("mission_id")},
        )

    async def _on_dismissed(self, event: EventEnvelope) -> None:
        key = str(event.payload.get("notice_key", "")).strip()
        if not key:
            return
        self.memory.remember(
            MemoryKind.PROCEDURAL,
            f"proactive suppression {key}",
            {"notice_key": key, "dismissed": True},
            importance=0.9,
            confidence=1.0,
            source="owner",
            ttl_seconds=30 * 24 * 60 * 60,
        )
