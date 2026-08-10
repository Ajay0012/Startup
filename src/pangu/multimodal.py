from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from math import exp


class Modality(StrEnum):
    VOICE = "voice"
    SCREEN = "screen"
    CAMERA = "camera"
    GESTURE = "gesture"
    BROWSER = "browser"
    WINDOWS = "windows"
    MEMORY = "memory"
    FILE = "file"
    MISSION = "mission"


@dataclass(frozen=True)
class ContextSignal:
    modality: Modality
    kind: str
    value: object
    confidence: float = 1.0
    observed_at: float = field(default_factory=time.monotonic)
    target_id: str | None = None
    source: str = "runtime"
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("signal kind is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class GroundedReferent:
    target_id: str
    modality: Modality
    kind: str
    value: object
    score: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class FusedContext:
    signals: tuple[ContextSignal, ...]
    referent: GroundedReferent | None
    prompt_context: tuple[str, ...]


class MultimodalContextFusion:
    """Bounded recency/confidence fusion for shared PANGU context.

    This layer does not execute actions. It gives language/model layers grounded
    referents for phrases such as "that", "there", "the other one" using recent
    screen, gesture, browser and window evidence. Sensitive signals are omitted from
    cloud prompt context unless explicitly allowed by the caller.
    """

    _deictic_terms = frozenset(
        {"that", "this", "it", "there", "that one", "this one", "the one", "that window"}
    )
    _target_modalities = frozenset(
        {Modality.GESTURE, Modality.SCREEN, Modality.BROWSER, Modality.WINDOWS, Modality.CAMERA}
    )

    def __init__(self, *, max_signals: int = 128, half_life_seconds: float = 8.0) -> None:
        if not 8 <= max_signals <= 4096:
            raise ValueError("max_signals must be between 8 and 4096")
        if not 0.5 <= half_life_seconds <= 300:
            raise ValueError("half_life_seconds must be between 0.5 and 300")
        self.max_signals = max_signals
        self.half_life_seconds = half_life_seconds
        self._signals: list[ContextSignal] = []

    def observe(self, signal: ContextSignal) -> None:
        self._signals.append(signal)
        if len(self._signals) > self.max_signals:
            del self._signals[: len(self._signals) - self.max_signals]

    def extend(self, signals: tuple[ContextSignal, ...]) -> None:
        for signal in signals:
            self.observe(signal)

    def recent(self, *, seconds: float = 30.0) -> tuple[ContextSignal, ...]:
        now = time.monotonic()
        return tuple(item for item in self._signals if now - item.observed_at <= seconds)

    def _recency(self, signal: ContextSignal, now: float) -> float:
        age = max(0.0, now - signal.observed_at)
        return exp(-0.69314718056 * age / self.half_life_seconds)

    def resolve_referent(self, utterance: str) -> GroundedReferent | None:
        normalized = " ".join(utterance.casefold().split())
        if not any(term in normalized for term in self._deictic_terms):
            return None
        now = time.monotonic()
        candidates: list[GroundedReferent] = []
        for signal in self._signals:
            if signal.target_id is None or signal.modality not in self._target_modalities:
                continue
            recency = self._recency(signal, now)
            modality_bonus = {
                Modality.GESTURE: 0.24,
                Modality.SCREEN: 0.16,
                Modality.BROWSER: 0.12,
                Modality.WINDOWS: 0.10,
                Modality.CAMERA: 0.08,
            }.get(signal.modality, 0.0)
            score = min(1.0, signal.confidence * 0.65 + recency * 0.25 + modality_bonus)
            evidence = (
                f"modality={signal.modality.value}",
                f"kind={signal.kind}",
                f"confidence={signal.confidence:.2f}",
                f"recency={recency:.2f}",
            )
            candidates.append(
                GroundedReferent(
                    signal.target_id,
                    signal.modality,
                    signal.kind,
                    signal.value,
                    score,
                    evidence,
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.score, reverse=True)
        best = candidates[0]
        if best.score < 0.56:
            return None
        if len(candidates) > 1 and best.score - candidates[1].score < 0.04:
            return None
        return best

    def fuse(
        self,
        utterance: str,
        *,
        max_context_items: int = 16,
        allow_sensitive_cloud_context: bool = False,
    ) -> FusedContext:
        if not 1 <= max_context_items <= 64:
            raise ValueError("max_context_items must be between 1 and 64")
        now = time.monotonic()
        recent = [item for item in self._signals if now - item.observed_at <= 60]
        ranked = sorted(
            recent,
            key=lambda item: item.confidence * self._recency(item, now),
            reverse=True,
        )
        selected = tuple(ranked[:max_context_items])
        prompt: list[str] = []
        for item in selected:
            if item.sensitive and not allow_sensitive_cloud_context:
                continue
            rendered = str(item.value)
            if len(rendered) > 600:
                rendered = rendered[:597] + "..."
            prompt.append(
                f"[{item.modality.value}:{item.kind}] {rendered} "
                f"(confidence={item.confidence:.2f}, source={item.source})"
            )
        return FusedContext(selected, self.resolve_referent(utterance), tuple(prompt))
