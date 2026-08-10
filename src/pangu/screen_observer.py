from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from time import monotonic

from .events import EventBus, EventEnvelope, EventPriority
from .multimodal import ContextSignal, Modality, MultimodalContextFusion
from .screen_perception import ScreenPerceptionRuntime, ScreenSnapshot
from .screen_vision import ScreenVisionRuntime


@dataclass(frozen=True)
class ScreenObservationPolicy:
    enabled: bool = False
    interval_seconds: float = 1.25
    history_size: int = 120
    maximum_elements: int = 80
    ocr_enabled: bool = True
    suppress_password_contexts: bool = True
    deduplicate_unchanged: bool = True

    def __post_init__(self) -> None:
        if not 0.25 <= self.interval_seconds <= 30:
            raise ValueError("screen observation interval must be between 0.25 and 30 seconds")
        if not 10 <= self.history_size <= 2000:
            raise ValueError("screen history size must be between 10 and 2000")
        if not 10 <= self.maximum_elements <= 500:
            raise ValueError("maximum screen elements must be between 10 and 500")


@dataclass(frozen=True)
class SemanticScreenObservation:
    observed_at: float
    active_window_title: str | None
    active_window_handle: int | None
    element_labels: tuple[str, ...]
    ocr_text: tuple[str, ...]
    sensitive: bool
    verification_state: str
    fingerprint: str


class ScreenObservationRuntime:
    """Continuously observe semantic screen state without retaining screenshots.

    Raw screenshot bytes exist only inside the optional local ScreenVisionRuntime call and
    are dropped immediately after OCR. This component persists no pixels. Password/sensitive
    UIA contexts are suppressed rather than summarized into shared context.
    """

    def __init__(
        self,
        perception: ScreenPerceptionRuntime,
        fusion: MultimodalContextFusion,
        events: EventBus,
        *,
        vision: ScreenVisionRuntime | None = None,
        policy: ScreenObservationPolicy | None = None,
    ) -> None:
        self.perception = perception
        self.fusion = fusion
        self.events = events
        self.vision = vision
        self.policy = policy or ScreenObservationPolicy()
        self._history: deque[SemanticScreenObservation] = deque(maxlen=self.policy.history_size)
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._paused = False
        self._pause_reason: str | None = None
        self._last_fingerprint: str | None = None

    @property
    def history(self) -> tuple[SemanticScreenObservation, ...]:
        return tuple(self._history)

    def pause(self, reason: str = "owner") -> None:
        self._paused = True
        self._pause_reason = reason[:128]

    def resume(self) -> None:
        self._paused = False
        self._pause_reason = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self.policy.enabled:
            self._task = asyncio.create_task(self._loop(), name="pangu-screen-observer")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @staticmethod
    def _is_sensitive(snapshot: ScreenSnapshot) -> bool:
        return any(item.visible and item.is_password for item in snapshot.elements)

    def _semantic_labels(self, snapshot: ScreenSnapshot) -> tuple[str, ...]:
        labels: list[str] = []
        seen: set[str] = set()
        for element in snapshot.elements:
            if not element.visible or not element.enabled or element.is_password:
                continue
            value = " ".join(element.name.strip().split())
            if not value:
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            labels.append(value[:160])
            if len(labels) >= self.policy.maximum_elements:
                break
        return tuple(labels)

    @staticmethod
    def _fingerprint(
        snapshot: ScreenSnapshot,
        labels: tuple[str, ...],
        ocr_text: tuple[str, ...],
        sensitive: bool,
    ) -> str:
        return "|".join(
            (
                str(snapshot.active_window_handle or 0),
                snapshot.active_window_title or "",
                "1" if sensitive else "0",
                "\x1f".join(labels),
                "\x1f".join(ocr_text),
            )
        )

    async def observe_once(self) -> SemanticScreenObservation | None:
        if self._paused:
            await self.events.publish(
                EventEnvelope(
                    "screen.observation.paused",
                    {"reason": self._pause_reason or "paused"},
                    EventPriority.LOW,
                )
            )
            return None
        snapshot = await asyncio.to_thread(self.perception.capture)
        sensitive = self.policy.suppress_password_contexts and self._is_sensitive(snapshot)
        labels = () if sensitive else self._semantic_labels(snapshot)
        ocr_text: tuple[str, ...] = ()
        if (
            not sensitive
            and self.policy.ocr_enabled
            and self.vision is not None
            and snapshot.verification_state == "VERIFIED"
        ):
            try:
                _frame, regions = await asyncio.to_thread(self.vision.snapshot)
                ocr_text = tuple(
                    " ".join(region.text.split())[:240]
                    for region in regions[:80]
                    if region.confidence >= 0.45 and region.text.strip()
                )
            except (RuntimeError, OSError, ValueError):
                ocr_text = ()

        fingerprint = self._fingerprint(snapshot, labels, ocr_text, sensitive)
        if self.policy.deduplicate_unchanged and fingerprint == self._last_fingerprint:
            return None
        self._last_fingerprint = fingerprint
        observation = SemanticScreenObservation(
            monotonic(),
            None if sensitive else snapshot.active_window_title,
            snapshot.active_window_handle,
            labels,
            ocr_text,
            sensitive,
            snapshot.verification_state,
            fingerprint,
        )
        self._history.append(observation)
        if sensitive:
            await self.events.publish(
                EventEnvelope(
                    "screen.observation.suppressed",
                    {
                        "active_window_handle": snapshot.active_window_handle,
                        "reason": "SENSITIVE_UI_CONTEXT",
                    },
                    EventPriority.LOW,
                )
            )
            return observation

        target_id = (
            f"window:{snapshot.active_window_handle}"
            if snapshot.active_window_handle is not None
            else None
        )
        self.fusion.observe(
            ContextSignal(
                Modality.SCREEN,
                "active_window",
                {
                    "title": snapshot.active_window_title,
                    "labels": labels[:30],
                    "ocr": ocr_text[:30],
                },
                confidence=0.92 if snapshot.verification_state == "VERIFIED" else 0.55,
                target_id=target_id,
                source="screen-observer",
            )
        )
        await self.events.publish(
            EventEnvelope(
                "screen.observation.changed",
                {
                    "active_window_title": snapshot.active_window_title,
                    "active_window_handle": snapshot.active_window_handle,
                    "element_count": len(labels),
                    "ocr_region_count": len(ocr_text),
                    "verification_state": snapshot.verification_state,
                    "raw_pixels_persisted": False,
                },
                EventPriority.LOW,
            )
        )
        return observation

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.observe_once()
            except (RuntimeError, OSError, ValueError):
                await self.events.publish(
                    EventEnvelope(
                        "screen.observation.failed",
                        {"normalized_error": "SCREEN_OBSERVATION_FAILED"},
                        EventPriority.LOW,
                    )
                )
            await asyncio.sleep(self.policy.interval_seconds)
