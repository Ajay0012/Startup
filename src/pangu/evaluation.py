from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from statistics import mean


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher"
    LOWER_IS_BETTER = "lower"


@dataclass(frozen=True)
class BenchmarkMetric:
    name: str
    value: float
    direction: MetricDirection
    weight: float = 1.0
    hard_minimum: float | None = None
    hard_maximum: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("metric name is required")
        if self.weight <= 0:
            raise ValueError("metric weight must be positive")


@dataclass(frozen=True)
class IntelligenceBenchmark:
    label: str
    metrics: tuple[BenchmarkMetric, ...]

    def by_name(self) -> dict[str, BenchmarkMetric]:
        return {item.name: item for item in self.metrics}


@dataclass(frozen=True)
class EvaluationDecision:
    accepted: bool
    score_delta: float
    regressions: tuple[str, ...]
    improvements: tuple[str, ...]
    missing_metrics: tuple[str, ...]


class IntelligenceEvaluationGate:
    """Reject self-upgrades that regress protected intelligence/reliability metrics."""

    DEFAULT_PROTECTED = frozenset(
        {
            "wake_false_accept_rate",
            "wake_false_reject_rate",
            "transcription_word_error_rate",
            "command_completion_rate",
            "hallucination_rate",
            "computer_use_success_rate",
            "p95_turn_latency_ms",
            "mission_success_rate",
            "mission_recovery_rate",
            "memory_retrieval_precision",
            "crash_free_session_rate",
        }
    )

    def __init__(
        self,
        *,
        allowed_relative_regression: float = 0.02,
        protected_metrics: frozenset[str] | None = None,
    ) -> None:
        if not 0 <= allowed_relative_regression <= 0.2:
            raise ValueError("allowed_relative_regression must be between 0 and 0.2")
        self.allowed_relative_regression = allowed_relative_regression
        self.protected_metrics = protected_metrics or self.DEFAULT_PROTECTED

    @staticmethod
    def _utility(metric: BenchmarkMetric) -> float:
        return metric.value if metric.direction == MetricDirection.HIGHER_IS_BETTER else -metric.value

    @staticmethod
    def _violates_hard_limit(metric: BenchmarkMetric) -> bool:
        if metric.hard_minimum is not None and metric.value < metric.hard_minimum:
            return True
        if metric.hard_maximum is not None and metric.value > metric.hard_maximum:
            return True
        return False

    def compare(
        self,
        baseline: IntelligenceBenchmark,
        candidate: IntelligenceBenchmark,
    ) -> EvaluationDecision:
        old = baseline.by_name()
        new = candidate.by_name()
        missing = tuple(sorted(name for name in old if name not in new))
        regressions: list[str] = []
        improvements: list[str] = []
        weighted_deltas: list[float] = []
        for name, before in old.items():
            after = new.get(name)
            if after is None:
                continue
            if before.direction != after.direction:
                regressions.append(f"{name}: metric direction changed")
                continue
            if self._violates_hard_limit(after):
                regressions.append(f"{name}: hard quality limit violated")
                continue
            before_utility = self._utility(before)
            after_utility = self._utility(after)
            scale = max(abs(before.value), 1e-9)
            relative = (after_utility - before_utility) / scale
            weighted_deltas.append(relative * before.weight)
            if relative > 0.01:
                improvements.append(f"{name}: {relative:+.2%}")
            if name in self.protected_metrics and relative < -self.allowed_relative_regression:
                regressions.append(f"{name}: {relative:+.2%}")
        score_delta = mean(weighted_deltas) if weighted_deltas else 0.0
        accepted = not regressions and not missing and score_delta >= -self.allowed_relative_regression
        return EvaluationDecision(
            accepted,
            score_delta,
            tuple(regressions),
            tuple(improvements),
            missing,
        )


def standard_benchmark(
    *,
    wake_far: float,
    wake_frr: float,
    wer: float,
    command_completion: float,
    hallucination_rate: float,
    computer_use_success: float,
    p95_turn_latency_ms: float,
    mission_success: float,
    recovery_rate: float,
    memory_precision: float,
    crash_free_rate: float,
    label: str = "pangu",
) -> IntelligenceBenchmark:
    return IntelligenceBenchmark(
        label,
        (
            BenchmarkMetric("wake_false_accept_rate", wake_far, MetricDirection.LOWER_IS_BETTER, 1.2, hard_maximum=0.08),
            BenchmarkMetric("wake_false_reject_rate", wake_frr, MetricDirection.LOWER_IS_BETTER, 1.0, hard_maximum=0.20),
            BenchmarkMetric("transcription_word_error_rate", wer, MetricDirection.LOWER_IS_BETTER, 1.2, hard_maximum=0.25),
            BenchmarkMetric("command_completion_rate", command_completion, MetricDirection.HIGHER_IS_BETTER, 1.5, hard_minimum=0.85),
            BenchmarkMetric("hallucination_rate", hallucination_rate, MetricDirection.LOWER_IS_BETTER, 1.5, hard_maximum=0.08),
            BenchmarkMetric("computer_use_success_rate", computer_use_success, MetricDirection.HIGHER_IS_BETTER, 1.3, hard_minimum=0.75),
            BenchmarkMetric("p95_turn_latency_ms", p95_turn_latency_ms, MetricDirection.LOWER_IS_BETTER, 1.1, hard_maximum=4500),
            BenchmarkMetric("mission_success_rate", mission_success, MetricDirection.HIGHER_IS_BETTER, 1.3, hard_minimum=0.75),
            BenchmarkMetric("mission_recovery_rate", recovery_rate, MetricDirection.HIGHER_IS_BETTER, 1.0, hard_minimum=0.60),
            BenchmarkMetric("memory_retrieval_precision", memory_precision, MetricDirection.HIGHER_IS_BETTER, 1.0, hard_minimum=0.75),
            BenchmarkMetric("crash_free_session_rate", crash_free_rate, MetricDirection.HIGHER_IS_BETTER, 1.8, hard_minimum=0.98),
        ),
    )
