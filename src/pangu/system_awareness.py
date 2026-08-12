from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol

from .events import EventBus, EventEnvelope
from .world_model import PersonalWorldModel


@dataclass(frozen=True)
class SystemSnapshot:
    battery_percent: float | None
    plugged_in: bool | None
    cpu_percent: float
    memory_percent: float
    network_available: bool


class SystemProbe(Protocol):
    def snapshot(self) -> SystemSnapshot: ...


class PsutilSystemProbe:
    """Read-only local telemetry probe. No packet contents or process secrets are collected."""

    def snapshot(self) -> SystemSnapshot:
        psutil = import_module("psutil")
        battery = psutil.sensors_battery()
        stats = psutil.net_if_stats()
        network_available = any(bool(item.isup) for item in stats.values()) if stats else False
        return SystemSnapshot(
            float(battery.percent) if battery is not None else None,
            bool(battery.power_plugged) if battery is not None else None,
            float(psutil.cpu_percent(interval=None)),
            float(psutil.virtual_memory().percent),
            network_available,
        )


class SystemAwarenessRuntime:
    """Periodic local sensor feeding verified deltas into the shared world model/EventBus."""

    def __init__(
        self,
        world: PersonalWorldModel,
        events: EventBus,
        probe: SystemProbe | None = None,
        interval_seconds: float = 5.0,
    ) -> None:
        if not 1 <= interval_seconds <= 300:
            raise ValueError("awareness interval must be between 1 and 300 seconds")
        self.world = world
        self.events = events
        self.probe = probe or PsutilSystemProbe()
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="pangu-system-awareness")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def sample_once(self) -> SystemSnapshot:
        snapshot = await asyncio.to_thread(self.probe.snapshot)
        await self._observe(
            "system.battery",
            "percent",
            snapshot.battery_percent,
            0.95,
            0.95
            if snapshot.battery_percent is not None and snapshot.battery_percent <= 15
            else 0.4,
            "Battery is critically low."
            if snapshot.battery_percent is not None and snapshot.battery_percent <= 15
            else "Battery level changed.",
        )
        await self._observe(
            "system.power",
            "plugged_in",
            snapshot.plugged_in,
            1.0,
            0.65,
            "Power source changed.",
        )
        await self._observe(
            "system.performance",
            "cpu_percent",
            round(snapshot.cpu_percent, 1),
            0.9,
            0.8 if snapshot.cpu_percent >= 90 else 0.3,
            "CPU usage is very high." if snapshot.cpu_percent >= 90 else "CPU usage changed.",
            change_threshold=10.0,
        )
        await self._observe(
            "system.performance",
            "memory_percent",
            round(snapshot.memory_percent, 1),
            0.9,
            0.85 if snapshot.memory_percent >= 90 else 0.3,
            "Memory pressure is very high."
            if snapshot.memory_percent >= 90
            else "Memory usage changed.",
            change_threshold=10.0,
        )
        await self._observe(
            "system.network",
            "available",
            snapshot.network_available,
            0.9,
            0.9 if not snapshot.network_available else 0.55,
            "Network connectivity is unavailable."
            if not snapshot.network_available
            else "Network connectivity was restored.",
        )
        return snapshot

    async def _observe(
        self,
        entity: str,
        attribute: str,
        value: object,
        confidence: float,
        importance: float,
        message: str,
        change_threshold: float | None = None,
    ) -> None:
        previous = self.world.get(entity, attribute)
        if change_threshold is not None and previous is not None:
            try:
                if abs(float(previous.value) - float(value)) < change_threshold:
                    return
            except (TypeError, ValueError):
                pass
        delta = self.world.observe(entity, attribute, value, confidence=confidence, source="system")
        if not delta.changed:
            return
        await self.events.publish(
            EventEnvelope(
                "world.delta",
                {
                    "entity": entity,
                    "attribute": attribute,
                    "previous": delta.previous,
                    "current": value,
                    "changed": True,
                    "confidence": confidence,
                    "source": "system",
                    "importance": importance,
                    "message": message,
                },
            )
        )

    async def _run(self) -> None:
        while self._running:
            try:
                await self.sample_once()
            except (RuntimeError, OSError, ImportError, ModuleNotFoundError):
                await self.events.publish(
                    EventEnvelope(
                        "awareness.sensor.failed",
                        {"sensor": "system", "retryable": True},
                    )
                )
            await asyncio.sleep(self.interval_seconds)
