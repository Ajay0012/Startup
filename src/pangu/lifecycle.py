from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class LifecycleState(StrEnum):
    REGISTERED = "REGISTERED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class LifecycleService:
    name: str
    start: Callable[[], Awaitable[None]]
    stop: Callable[[], Awaitable[None]]
    dependencies: tuple[str, ...] = ()


class LifecycleKernel:
    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self.state = LifecycleState.REGISTERED
        self._services: dict[str, LifecycleService] = {}
        self._started: list[LifecycleService] = []
        self.timeout_seconds = timeout_seconds

    def register(self, service: LifecycleService) -> None:
        if self.state != LifecycleState.REGISTERED or service.name in self._services:
            raise ValueError("service registration is closed or duplicated")
        self._services[service.name] = service

    def _ordered(self) -> list[LifecycleService]:
        remaining = dict(self._services)
        ordered: list[LifecycleService] = []
        while remaining:
            ready = [
                item
                for item in remaining.values()
                if set(item.dependencies) <= {s.name for s in ordered}
            ]
            if not ready:
                raise RuntimeError("lifecycle dependency cycle or missing dependency")
            for service in ready:
                ordered.append(service)
                del remaining[service.name]
        return ordered

    async def start(self) -> None:
        self.state = LifecycleState.STARTING
        try:
            for service in self._ordered():
                await asyncio.wait_for(service.start(), timeout=self.timeout_seconds)
                self._started.append(service)
            self.state = LifecycleState.RUNNING
        except Exception:
            self.state = LifecycleState.FAILED
            await self.stop()
            raise

    async def stop(self) -> None:
        if self.state == LifecycleState.STOPPED:
            return
        self.state = LifecycleState.STOPPING
        failures: list[Exception] = []
        for service in reversed(self._started):
            try:
                await asyncio.wait_for(service.stop(), timeout=self.timeout_seconds)
            except (TimeoutError, RuntimeError, OSError) as error:
                failures.append(error)
        self._started.clear()
        self.state = LifecycleState.STOPPED
        if failures:
            raise ExceptionGroup("shutdown failures", failures)
