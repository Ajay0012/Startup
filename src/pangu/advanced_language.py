from __future__ import annotations

from .contextual_nlu import ContextualLanguageResolver
from .contracts import NormalizedIntent
from .language import LanguageRuntime


class ContextAwareLanguageRuntime(LanguageRuntime):
    """Extend deterministic language parsing with bounded conversational references."""

    def __init__(self, resolver: ContextualLanguageResolver) -> None:
        super().__init__()
        self.resolver = resolver

    def normalize(self, text: str) -> NormalizedIntent:
        clean = " ".join(text.strip().split())
        base = super().normalize(clean)
        if base.intent_name != "informational":
            self.resolver.remember_turn(clean)
            return base

        contextual = self.resolver.resolve(clean)
        if contextual.resolved_text != clean and contextual.used_history:
            repeated = super().normalize(contextual.resolved_text)
            if repeated.intent_name != "informational":
                self.resolver.remember_turn(clean)
                return NormalizedIntent(
                    repeated.intent_name,
                    repeated.canonical_english,
                    clean,
                    dict(repeated.entities),
                    max(0.86, repeated.confidence),
                    repeated.detected_language,
                )

        canonical = contextual.resolved_text
        entities: dict[str, str] = {}
        if contextual.referent is not None:
            entities = {
                "referent_id": contextual.referent.target_id,
                "referent_kind": contextual.referent.kind,
                "referent_value": str(contextual.referent.value),
            }
        self.resolver.remember_turn(
            clean,
            contextual.referent.target_id if contextual.referent is not None else None,
        )
        return NormalizedIntent(
            "contextual_reference" if contextual.referent is not None else "informational",
            canonical,
            clean,
            entities,
            0.88 if contextual.referent is not None else base.confidence,
            base.detected_language,
        )
