from __future__ import annotations

import re
from dataclasses import dataclass

from .conversation_intelligence import ConversationAct, ConversationActClassifier
from .multimodal import GroundedReferent, MultimodalContextFusion


@dataclass(frozen=True)
class ContextualUtterance:
    original: str
    resolved_text: str
    act: ConversationAct
    referent: GroundedReferent | None
    used_history: bool


class ContextualLanguageResolver:
    """Resolve short conversational references before deterministic intent parsing."""

    _history_refs = re.compile(
        r"\b(previous|last|same|again|other|another|his|her|their|it|that|this|there)\b",
        re.IGNORECASE,
    )

    def __init__(self, fusion: MultimodalContextFusion, *, history_limit: int = 8) -> None:
        self.fusion = fusion
        self.classifier = ConversationActClassifier()
        self.history_limit = history_limit
        self._history: list[tuple[str, str | None]] = []

    def remember_turn(self, utterance: str, target_id: str | None = None) -> None:
        self._history.append((" ".join(utterance.strip().split()), target_id))
        if len(self._history) > self.history_limit:
            del self._history[: len(self._history) - self.history_limit]

    def resolve(self, utterance: str) -> ContextualUtterance:
        clean = " ".join(utterance.strip().split())
        act = self.classifier.classify(clean)
        referent = self.fusion.resolve_referent(clean)
        resolved = clean
        used_history = False
        if referent is not None and self._history_refs.search(clean):
            resolved = f"{clean} [resolved target={referent.target_id} label={referent.value}]"
        elif self._history and self._history_refs.search(clean):
            previous, target = self._history[-1]
            if target:
                resolved = f"{clean} [previous target={target}; previous utterance={previous}]"
                used_history = True
            elif clean.casefold() in {"do that again", "same again", "do the same", "again"}:
                resolved = previous
                used_history = True
        return ContextualUtterance(clean, resolved, act, referent, used_history)
