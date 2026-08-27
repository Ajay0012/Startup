from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class AttentionContext:
    local_time: datetime
    user_speaking: bool = False
    pangu_speaking: bool = False
    full_screen_app: bool = False
    presentation_mode: bool = False
    do_not_disturb: bool = False
    active_mission_priority: int = 0
    recent_interruptions: int = 0


@dataclass(frozen=True)
class ProactiveCandidate:
    subject: str
    message: str
    importance: float
    urgency: float
    novelty: float
    reversible: bool = True
    deadline_minutes: int | None = None

    def __post_init__(self) -> None:
        for value in (self.importance, self.urgency, self.novelty):
            if not 0 <= value <= 1:
                raise ValueError("proactive scores must be between 0 and 1")


@dataclass(frozen=True)
class InterruptionDecision:
    interrupt_now: bool
    score: float
    reason: str
    presentation: str


class ContextualInterruptionPolicy:
    """Decide when PANGU should interrupt instead of merely having something to say."""

    def __init__(
        self,
        *,
        quiet_start: time = time(22, 30),
        quiet_end: time = time(7, 0),
        interrupt_threshold: float = 0.72,
    ) -> None:
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.interrupt_threshold = interrupt_threshold

    def _quiet(self, value: time) -> bool:
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= value < self.quiet_end
        return value >= self.quiet_start or value < self.quiet_end

    def decide(
        self, candidate: ProactiveCandidate, context: AttentionContext
    ) -> InterruptionDecision:
        if context.do_not_disturb and candidate.urgency < 0.95:
            return InterruptionDecision(False, 0.0, "do-not-disturb", "silent")
        if (context.user_speaking or context.pangu_speaking) and candidate.urgency < 0.9:
            return InterruptionDecision(False, 0.0, "conversation-in-progress", "queue")
        score = candidate.importance * 0.42 + candidate.urgency * 0.40 + candidate.novelty * 0.18
        if candidate.deadline_minutes is not None:
            if candidate.deadline_minutes <= 5:
                score += 0.20
            elif candidate.deadline_minutes <= 15:
                score += 0.10
        if context.presentation_mode or context.full_screen_app:
            score -= 0.20
        if self._quiet(context.local_time.time()) and candidate.urgency < 0.9:
            score -= 0.22
        score -= min(0.25, context.recent_interruptions * 0.05)
        if context.active_mission_priority >= 80 and candidate.urgency < 0.8:
            score -= 0.08
        score = max(0.0, min(1.0, score))
        interrupt = score >= self.interrupt_threshold
        presentation = (
            "voice+hud" if interrupt and score >= 0.86 else "hud" if interrupt else "queue"
        )
        return InterruptionDecision(
            interrupt,
            score,
            "high-value context change" if interrupt else "defer until attention is available",
            presentation,
        )
