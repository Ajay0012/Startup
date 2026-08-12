from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from .memory import MemoryKind, MemoryRecord, PersistentMemoryRuntime


@dataclass(frozen=True)
class ActionOutcome:
    capability: str
    operation: str
    arguments: dict[str, object]
    verified: bool
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class CapabilityGap:
    capability: str
    operation: str
    failure_count: int
    recent_errors: tuple[str, ...]
    recommendation: str


class AdaptiveLearningRuntime:
    """Learn from verified outcomes without changing source code automatically.

    Successful repeated action shapes become procedural memory. Repeated failures become
    capability-gap memories that can ground a later owner-directed self-upgrade request.
    This runtime never edits code and never converts observations into permissions.
    """

    def __init__(self, memory: PersistentMemoryRuntime) -> None:
        self.memory = memory

    @staticmethod
    def _signature(outcome: ActionOutcome) -> str:
        keys = ",".join(sorted(outcome.arguments))
        return f"{outcome.capability}:{outcome.operation}:{keys}"

    @staticmethod
    def _subject(prefix: str, signature: str) -> str:
        digest = sha256(signature.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}:{digest}"

    def record_outcome(self, outcome: ActionOutcome) -> MemoryRecord:
        if outcome.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        signature = self._signature(outcome)
        history = self.memory.recall(
            signature,
            kinds=(MemoryKind.EPISODIC,),
            limit=20,
        )
        previous_successes = sum(bool(item.content.get("verified")) for item in history)
        previous_failures = sum(not bool(item.content.get("verified")) for item in history)
        sequence = previous_successes + previous_failures + 1
        record = self.memory.remember(
            MemoryKind.EPISODIC,
            self._subject(f"outcome-{sequence}", signature),
            {
                "signature": signature,
                "capability": outcome.capability,
                "operation": outcome.operation,
                "argument_keys": sorted(outcome.arguments),
                "verified": outcome.verified,
                "latency_ms": outcome.latency_ms,
                "error": outcome.error,
            },
            importance=0.65 if not outcome.verified else 0.45,
            confidence=1.0,
            source="adaptive-learning",
        )
        if outcome.verified and previous_successes + 1 >= 3:
            self._learn_procedure(outcome, previous_successes + 1)
        if not outcome.verified and previous_failures + 1 >= 3:
            self._learn_gap(outcome, previous_failures + 1, history)
        return record

    def _learn_procedure(self, outcome: ActionOutcome, samples: int) -> MemoryRecord:
        signature = self._signature(outcome)
        return self.memory.remember(
            MemoryKind.PROCEDURAL,
            self._subject("procedure", signature),
            {
                "signature": signature,
                "capability": outcome.capability,
                "operation": outcome.operation,
                "argument_keys": sorted(outcome.arguments),
                "verified_samples": samples,
                "policy": "reuse only through the original verified capability boundary",
            },
            importance=min(0.95, 0.6 + samples * 0.03),
            confidence=min(0.98, 0.65 + samples * 0.05),
            source="procedure-induction",
        )

    def _learn_gap(
        self,
        outcome: ActionOutcome,
        failures: int,
        history: Iterable[MemoryRecord],
    ) -> MemoryRecord:
        signature = self._signature(outcome)
        errors = [
            str(item.content.get("error"))
            for item in history
            if not bool(item.content.get("verified")) and item.content.get("error")
        ]
        if outcome.error:
            errors.append(outcome.error)
        return self.memory.remember(
            MemoryKind.SEMANTIC,
            self._subject("capability-gap", signature),
            {
                "signature": signature,
                "capability": outcome.capability,
                "operation": outcome.operation,
                "failure_count": failures,
                "recent_errors": errors[-5:],
                "recommendation": (
                    "Investigate and improve this capability only when the owner requests an upgrade."
                ),
            },
            importance=min(1.0, 0.7 + failures * 0.04),
            confidence=min(0.98, 0.6 + failures * 0.05),
            source="capability-gap-detector",
        )

    def capability_gaps(
        self, query: str = "capability gap", limit: int = 10
    ) -> tuple[CapabilityGap, ...]:
        records = self.memory.recall(query, kinds=(MemoryKind.SEMANTIC,), limit=limit)
        gaps: list[CapabilityGap] = []
        for item in records:
            if not item.subject.startswith("capability-gap:"):
                continue
            gaps.append(
                CapabilityGap(
                    str(item.content.get("capability", "unknown")),
                    str(item.content.get("operation", "unknown")),
                    int(item.content.get("failure_count", 0)),
                    tuple(str(value) for value in item.content.get("recent_errors", [])),
                    str(item.content.get("recommendation", "")),
                )
            )
        return tuple(gaps)
