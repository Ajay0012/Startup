from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class EventPriority(StrEnum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


@dataclass(frozen=True)
class EventEnvelope:
    event_type: str
    payload: dict[str, Any]
    priority: EventPriority = EventPriority.NORMAL
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    trace_id: str | None = None
    version: int = 1

    @property
    def topic(self) -> str:
        """Backward-compatible alias for subscribers that predate ``event_type``."""
        return self.event_type


EventHandler = Callable[[EventEnvelope], Awaitable[None]]


class EventBus:
    """One bounded async event owner with isolated subscriber failures."""

    def __init__(self, capacity: int = 128, handler_timeout: float = 2.0) -> None:
        self._queue: asyncio.PriorityQueue[tuple[int, int, EventEnvelope]] = asyncio.PriorityQueue(
            capacity
        )
        self._handlers: dict[str, list[EventHandler]] = {}
        self._sequence = 0
        self._worker: asyncio.Task[None] | None = None
        self._running = False
        self.dead_letters: list[EventEnvelope] = []
        self.handler_timeout = handler_timeout

    @property
    def running(self) -> bool:
        """Whether the single EventBus worker is currently accepting events."""
        return self._running

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        handlers = self._handlers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        handlers = self._handlers.get(event_type)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return
        if not handlers:
            self._handlers.pop(event_type, None)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker = asyncio.create_task(self._run(), name="pangu-event-bus")

    async def publish(self, event: EventEnvelope) -> None:
        if not self._running:
            raise RuntimeError("event bus is not running")
        priority = {EventPriority.HIGH: 0, EventPriority.NORMAL: 1, EventPriority.LOW: 2}[
            event.priority
        ]
        self._sequence += 1
        await self._queue.put((priority, self._sequence, event))

    async def _run(self) -> None:
        while self._running or not self._queue.empty():
            try:
                _, _, event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            try:
                for handler in tuple(self._handlers.get(event.event_type, [])):
                    try:
                        await asyncio.wait_for(handler(event), timeout=self.handler_timeout)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # A subscriber is not allowed to terminate the single EventBus worker.
                        # Preserve the envelope for diagnostics while continuing other events.
                        self.dead_letters.append(event)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        if not self._running:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=self.handler_timeout + 1.0)
        except TimeoutError:
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
        self._running = False
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
