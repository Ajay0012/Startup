from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .events import EventBus, EventEnvelope, EventPriority
from .resilience import ResilientLoadManager, SelfHealingSupervisor, ServiceHealth


@dataclass(frozen=True)
class ResiliencePolicy:
    interval_seconds: float = 5.0
    overload_queue_ratio: float = 0.8

    def __post_init__(self) -> None:
        if not 0.5 <= self.interval_seconds <= 300:
            raise ValueError("interval_seconds must be between 0.5 and 300")
        if not 0.1 <= self.overload_queue_ratio <= 1:
            raise ValueError("overload_queue_ratio must be between 0.1 and 1")


class ResilienceRuntime:
    """Active health/load telemetry and bounded self-healing lifecycle service."""

    def __init__(
        self,
        events: EventBus,
        supervisor: SelfHealingSupervisor,
        loads: dict[str, ResilientLoadManager[object]],
        policy: ResiliencePolicy | None = None,
    ) -> None:
        self.events = events
        self.supervisor = supervisor
        self.loads = dict(loads)
        self.policy = policy or ResiliencePolicy()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_health: dict[str, ServiceHealth] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="pangu-resilience-monitor")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            await self.check_once()
            await asyncio.sleep(self.policy.interval_seconds)

    async def check_once(self) -> None:
        health = await self.supervisor.check_once()
        for name, state in health.items():
            previous = self._last_health.get(name)
            if previous != state:
                await self.events.publish(
                    EventEnvelope(
                        "resilience.health.changed",
                        {
                            "service": name,
                            "previous": previous.value if previous is not None else None,
                            "state": state.value,
                        },
                        EventPriority.LOW if state == ServiceHealth.HEALTHY else EventPriority.HIGH,
                    )
                )
        self._last_health = dict(health)
        for name, manager in self.loads.items():
            snapshot = manager.snapshot()
            unhealthy = [
                endpoint.name
                for endpoint in snapshot.endpoints
                if endpoint.health == ServiceHealth.UNHEALTHY
            ]
            if snapshot.rejected or unhealthy:
                await self.events.publish(
                    EventEnvelope(
                        "resilience.load.degraded",
                        {
                            "pool": name,
                            "queued": snapshot.queued,
                            "in_flight": snapshot.in_flight,
                            "accepted": snapshot.accepted,
                            "completed": snapshot.completed,
                            "rejected": snapshot.rejected,
                            "unhealthy_endpoints": unhealthy,
                        },
                        EventPriority.HIGH,
                    )
                )
