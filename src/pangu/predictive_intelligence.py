from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class PredictionImpact(StrEnum):
    OBSERVE = "observe"
    SUGGEST = "suggest"
    LOW_IMPACT = "low_impact"
    CONSEQUENTIAL = "consequential"


@dataclass(frozen=True)
class RoutineEvent:
    kind: str
    value: str
    occurred_at: datetime


@dataclass(frozen=True)
class Prediction:
    action: str
    confidence: float
    impact: PredictionImpact
    rationale: str
    execute_directly: bool = False


class PredictiveBehaviorRuntime:
    """Learn short routine transitions and emit reversible predictions only."""

    def __init__(self, *, history_limit: int = 512, minimum_support: int = 3) -> None:
        if not 32 <= history_limit <= 10_000:
            raise ValueError("history_limit must be between 32 and 10000")
        if not 2 <= minimum_support <= 100:
            raise ValueError("minimum_support must be between 2 and 100")
        self.history: deque[RoutineEvent] = deque(maxlen=history_limit)
        self.minimum_support = minimum_support
        self._transitions: dict[str, Counter[str]] = defaultdict(Counter)
        self._dismissed: Counter[str] = Counter()

    def observe(self, kind: str, value: str, occurred_at: datetime | None = None) -> None:
        clean_kind = kind.strip().casefold()
        clean_value = " ".join(value.strip().split())
        if not clean_kind or not clean_value:
            raise ValueError("kind and value are required")
        event = RoutineEvent(clean_kind, clean_value, occurred_at or datetime.now(UTC))
        if self.history:
            previous = self.history[-1]
            key = f"{previous.kind}:{previous.value}"
            self._transitions[key][f"{event.kind}:{event.value}"] += 1
        self.history.append(event)

    def dismiss(self, action: str) -> None:
        self._dismissed[action] += 1

    def predict_next(self) -> Prediction | None:
        if not self.history:
            return None
        previous = self.history[-1]
        key = f"{previous.kind}:{previous.value}"
        counts = self._transitions.get(key)
        if not counts:
            return None
        action, support = counts.most_common(1)[0]
        total = sum(counts.values())
        if support < self.minimum_support:
            return None
        confidence = support / total
        dismissals = self._dismissed[action]
        confidence *= 1 / (1 + dismissals * 0.7)
        if confidence < 0.62:
            return None
        next_kind, _, next_value = action.partition(":")
        low_impact_kinds = {"open_app", "surface_project", "prepare_workspace", "show_document"}
        impact = (
            PredictionImpact.LOW_IMPACT
            if next_kind in low_impact_kinds
            else PredictionImpact.SUGGEST
        )
        return Prediction(
            action,
            confidence,
            impact,
            f"Observed {support}/{total} matching routine transitions after {previous.kind}:{previous.value}",
            False,
        )
