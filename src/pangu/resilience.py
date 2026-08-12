from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class ServiceHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay_seconds: float = 0.08
    max_delay_seconds: float = 1.5
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if not 1 <= self.attempts <= 10:
            raise ValueError("attempts must be between 1 and 10")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("invalid retry delays")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_seconds: float = 8.0
    half_open_successes_required: int = 2
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    half_open_successes: int = 0
    opened_at: float | None = None

    def allow(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self.opened_at is not None and current - self.opened_at >= self.recovery_seconds:
                self.state = CircuitState.HALF_OPEN
                self.half_open_successes = 0
                return True
            return False
        return True

    def success(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            self.half_open_successes += 1
            if self.half_open_successes >= self.half_open_successes_required:
                self.state = CircuitState.CLOSED
                self.failures = 0
                self.half_open_successes = 0
                self.opened_at = None
        else:
            self.failures = 0

    def failure(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self.failures += 1
        if self.state == CircuitState.HALF_OPEN or self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = current
            self.half_open_successes = 0


@dataclass
class EndpointStats:
    name: str
    weight: int = 1
    in_flight: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    rejected: int = 0
    ewma_latency_ms: float = 0.0
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    @property
    def health(self) -> ServiceHealth:
        if self.breaker.state == CircuitState.OPEN:
            return ServiceHealth.UNHEALTHY
        total = self.successes + self.failures + self.timeouts
        if total >= 5 and (self.failures + self.timeouts) / total >= 0.3:
            return ServiceHealth.DEGRADED
        return ServiceHealth.HEALTHY


@dataclass(frozen=True)
class LoadManagerSnapshot:
    queued: int
    in_flight: int
    accepted: int
    completed: int
    rejected: int
    endpoints: tuple[EndpointStats, ...]


class OverloadedError(RuntimeError):
    pass


class CircuitOpenError(RuntimeError):
    pass


class ResilientLoadManager(Generic[T]):
    """Bounded async load manager for PANGU service calls.

    It combines queue admission control, concurrency bulkheads, weighted least-load
    routing, per-endpoint circuit breakers, timeouts and bounded retry/backoff.
    It is intentionally generic so Gemini, browser, perception, research and other
    remote/local workers can share the same resilience semantics without creating
    parallel lifecycle owners.
    """

    def __init__(
        self,
        endpoint_names: Sequence[str],
        *,
        max_concurrency: int = 8,
        max_queue: int = 64,
        endpoint_weights: dict[str, int] | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if not endpoint_names:
            raise ValueError("at least one endpoint is required")
        if not 1 <= max_concurrency <= 256 or not 0 <= max_queue <= 10_000:
            raise ValueError("invalid load limits")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._max_queue = max_queue
        self._queue_depth = 0
        self._accepted = 0
        self._completed = 0
        self._rejected = 0
        self._recent_failures: deque[float] = deque(maxlen=128)
        weights = endpoint_weights or {}
        self.endpoints = {
            name: EndpointStats(name, max(1, int(weights.get(name, 1)))) for name in endpoint_names
        }
        self.retry = retry or RetryPolicy()
        self._lock = asyncio.Lock()

    def _select(self, excluded: set[str] | None = None) -> EndpointStats:
        excluded_names = excluded or set()
        candidates = [
            item
            for item in self.endpoints.values()
            if item.name not in excluded_names and item.breaker.allow()
        ]
        if not candidates and excluded_names:
            candidates = [item for item in self.endpoints.values() if item.breaker.allow()]
        if not candidates:
            raise CircuitOpenError("all service endpoints have open circuit breakers")
        # Weighted least-load with latency as a deterministic tie-breaker.
        return min(
            candidates,
            key=lambda item: (
                item.in_flight / item.weight,
                max(0, item.ewma_latency_ms),
                item.name,
            ),
        )

    async def _admit(self) -> None:
        async with self._lock:
            in_flight = self._max_concurrency - self._semaphore._value
            if in_flight >= self._max_concurrency and self._queue_depth >= self._max_queue:
                self._rejected += 1
                raise OverloadedError("PANGU load manager is saturated; request shed safely")
            self._queue_depth += 1
            self._accepted += 1
        await self._semaphore.acquire()
        async with self._lock:
            self._queue_depth -= 1

    async def execute(
        self,
        operation: Callable[[str], Awaitable[T]],
        *,
        timeout_seconds: float = 20.0,
        retryable: tuple[type[BaseException], ...] = (TimeoutError, OSError, RuntimeError),
    ) -> T:
        if not 0.05 <= timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0.05 and 300")
        await self._admit()
        try:
            last_error: BaseException | None = None
            attempted: set[str] = set()
            for attempt in range(self.retry.attempts):
                endpoint = self._select(attempted)
                attempted.add(endpoint.name)
                endpoint.in_flight += 1
                started = time.monotonic()
                try:
                    result = await asyncio.wait_for(
                        operation(endpoint.name), timeout=timeout_seconds
                    )
                except TimeoutError as error:
                    endpoint.timeouts += 1
                    endpoint.breaker.failure()
                    self._recent_failures.append(time.monotonic())
                    last_error = TimeoutError(f"endpoint {endpoint.name} timed out")
                    if attempt + 1 >= self.retry.attempts:
                        raise last_error from error
                except retryable as error:
                    endpoint.failures += 1
                    endpoint.breaker.failure()
                    self._recent_failures.append(time.monotonic())
                    last_error = error
                    if attempt + 1 >= self.retry.attempts:
                        raise
                else:
                    endpoint.successes += 1
                    endpoint.breaker.success()
                    self._completed += 1
                    return result
                finally:
                    elapsed_ms = (time.monotonic() - started) * 1000
                    endpoint.ewma_latency_ms = (
                        elapsed_ms
                        if endpoint.ewma_latency_ms == 0
                        else endpoint.ewma_latency_ms * 0.8 + elapsed_ms * 0.2
                    )
                    endpoint.in_flight = max(0, endpoint.in_flight - 1)
                delay = min(
                    self.retry.max_delay_seconds,
                    self.retry.base_delay_seconds * (2**attempt),
                )
                jitter = delay * self.retry.jitter_ratio
                await asyncio.sleep(max(0.0, delay + random.uniform(-jitter, jitter)))
            assert last_error is not None
            raise RuntimeError("load manager exhausted retries") from last_error
        finally:
            self._semaphore.release()

    def snapshot(self) -> LoadManagerSnapshot:
        return LoadManagerSnapshot(
            self._queue_depth,
            sum(item.in_flight for item in self.endpoints.values()),
            self._accepted,
            self._completed,
            self._rejected,
            tuple(self.endpoints.values()),
        )


HealthProbe = Callable[[], Awaitable[bool]]
RecoveryAction = Callable[[], Awaitable[None]]


@dataclass
class SelfHealingService:
    name: str
    probe: HealthProbe
    recover: RecoveryAction
    failure_threshold: int = 3
    recovery_cooldown_seconds: float = 10.0
    consecutive_failures: int = 0
    last_recovery_at: float | None = None
    health: ServiceHealth = ServiceHealth.HEALTHY


class SelfHealingSupervisor:
    """Health supervision with bounded, cooldown-protected recovery actions."""

    def __init__(self, *, probe_timeout_seconds: float = 3.0) -> None:
        self._services: dict[str, SelfHealingService] = {}
        self.probe_timeout_seconds = probe_timeout_seconds

    def register(self, service: SelfHealingService) -> None:
        if service.name in self._services:
            raise ValueError(f"duplicate supervised service: {service.name}")
        self._services[service.name] = service

    async def check_once(self) -> dict[str, ServiceHealth]:
        now = time.monotonic()
        result: dict[str, ServiceHealth] = {}
        for service in self._services.values():
            try:
                healthy = await asyncio.wait_for(
                    service.probe(), timeout=self.probe_timeout_seconds
                )
            except (TimeoutError, OSError, RuntimeError):
                healthy = False
            if healthy:
                service.consecutive_failures = 0
                service.health = ServiceHealth.HEALTHY
                result[service.name] = service.health
                continue
            service.consecutive_failures += 1
            service.health = (
                ServiceHealth.UNHEALTHY
                if service.consecutive_failures >= service.failure_threshold
                else ServiceHealth.DEGRADED
            )
            cooldown_ok = (
                service.last_recovery_at is None
                or now - service.last_recovery_at >= service.recovery_cooldown_seconds
            )
            if service.health == ServiceHealth.UNHEALTHY and cooldown_ok:
                try:
                    await asyncio.wait_for(
                        service.recover(), timeout=self.probe_timeout_seconds * 3
                    )
                except (TimeoutError, OSError, RuntimeError):
                    pass
                service.last_recovery_at = now
            result[service.name] = service.health
        return result
