from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import StrEnum


class ConversationAct(StrEnum):
    COMMAND = "command"
    BACKCHANNEL = "backchannel"
    CONTINUE = "continue"
    REPAIR = "repair"
    CANCEL = "cancel"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class PartialHypothesis:
    text: str
    stable_prefix: str
    confidence: float
    is_final: bool
    observed_at: float = field(default_factory=time.monotonic)


class PartialTranscriptStabilizer:
    """Tracks streaming ASR hypotheses without committing unstable suffixes."""

    def __init__(self, stable_repetitions: int = 2) -> None:
        if not 1 <= stable_repetitions <= 5:
            raise ValueError("stable_repetitions must be between 1 and 5")
        self.stable_repetitions = stable_repetitions
        self._last_tokens: list[str] = []
        self._counts: list[int] = []

    def update(
        self, text: str, *, confidence: float = 1.0, is_final: bool = False
    ) -> PartialHypothesis:
        clean = " ".join(text.strip().split())
        tokens = clean.split()
        new_counts: list[int] = []
        for index, token in enumerate(tokens):
            if (
                index < len(self._last_tokens)
                and self._last_tokens[index].casefold() == token.casefold()
            ):
                previous = self._counts[index] if index < len(self._counts) else 0
                new_counts.append(previous + 1)
            else:
                new_counts.append(1)
        self._last_tokens = tokens
        self._counts = new_counts
        stable_len = len(tokens) if is_final else 0
        if not is_final:
            for index, count in enumerate(new_counts):
                if count >= self.stable_repetitions:
                    stable_len = index + 1
                else:
                    break
        return PartialHypothesis(clean, " ".join(tokens[:stable_len]), confidence, is_final)

    def reset(self) -> None:
        self._last_tokens.clear()
        self._counts.clear()


@dataclass(frozen=True)
class EndOfTurnDecision:
    should_end: bool
    score: float
    reason: str


class SemanticEndOfTurnDetector:
    """Combines silence, syntax and discourse cues instead of using silence alone."""

    _unfinished_suffixes = (
        "and",
        "or",
        "because",
        "then",
        "with",
        "from",
        "to",
        "if",
        "when",
        "which",
        "that",
    )
    _repair_prefixes = ("no ", "sorry ", "i mean ", "actually ", "wait ")

    def decide(
        self,
        text: str,
        *,
        silence_ms: float,
        asr_final: bool = False,
        vad_confidence: float = 1.0,
    ) -> EndOfTurnDecision:
        clean = " ".join(text.strip().split()).casefold()
        if not clean:
            return EndOfTurnDecision(False, 0.0, "no speech")
        score = min(0.45, max(0.0, silence_ms) / 1400.0 * 0.45)
        if asr_final:
            score += 0.25
        if clean.endswith((".", "?", "!")):
            score += 0.12
        if any(clean.endswith(" " + item) or clean == item for item in self._unfinished_suffixes):
            score -= 0.35
        if (
            any(clean.startswith(prefix) for prefix in self._repair_prefixes)
            and len(clean.split()) < 4
        ):
            score -= 0.15
        if len(clean.split()) >= 3:
            score += 0.08
        score *= max(0.4, min(1.0, vad_confidence))
        threshold = 0.58 if silence_ms < 700 else 0.48
        return EndOfTurnDecision(score >= threshold, max(0.0, min(1.0, score)), "semantic+silence")


class ConversationActClassifier:
    _backchannels = {
        "uh huh",
        "uh-huh",
        "mhm",
        "mm hmm",
        "okay",
        "ok",
        "right",
        "yeah",
        "yes",
        "got it",
    }
    _continue = {"continue", "go on", "keep going", "carry on", "tell me more"}
    _cancel = {"stop", "cancel", "never mind", "nevermind", "leave it"}
    _repair = re.compile(
        r"^(?:no|sorry|wait|actually|i mean|not that|the other one|no no)\b",
        re.IGNORECASE,
    )

    def classify(self, text: str) -> ConversationAct:
        clean = " ".join(text.strip().casefold().split())
        if not clean:
            return ConversationAct.INCOMPLETE
        if clean in self._backchannels:
            return ConversationAct.BACKCHANNEL
        if clean in self._continue:
            return ConversationAct.CONTINUE
        if clean in self._cancel:
            return ConversationAct.CANCEL
        if self._repair.search(clean):
            return ConversationAct.REPAIR
        if clean.endswith(
            tuple(" " + item for item in SemanticEndOfTurnDetector._unfinished_suffixes)
        ):
            return ConversationAct.INCOMPLETE
        return ConversationAct.COMMAND


@dataclass
class EchoReferenceGate:
    """Adaptive near-end speech gate using recent output-energy reference.

    This is not acoustic echo cancellation. It is a conservative admission gate used
    before barge-in so speaker leakage is less likely to be mistaken for owner speech.
    Hardware AEC remains dependent on the selected Windows audio stack/device.
    """

    multiplier: float = 1.75
    absolute_floor: float = 0.025
    decay: float = 0.88
    output_reference: float = 0.0

    def observe_output_energy(self, energy: float) -> None:
        bounded = max(0.0, min(1.0, energy))
        self.output_reference = max(bounded, self.output_reference * self.decay)

    def admit_near_end(self, input_energy: float, *, vad_speech: bool) -> bool:
        threshold = max(self.absolute_floor, self.output_reference * self.multiplier)
        return vad_speech and input_energy >= threshold


@dataclass
class ConversationRepairState:
    previous_utterance: str | None = None
    previous_target_id: str | None = None
    corrections: int = 0

    def apply(self, act: ConversationAct, utterance: str, target_id: str | None = None) -> None:
        if act == ConversationAct.REPAIR:
            self.corrections += 1
        self.previous_utterance = utterance
        if target_id is not None:
            self.previous_target_id = target_id
