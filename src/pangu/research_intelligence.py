from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse


class SourceTier(StrEnum):
    PRIMARY = "primary"
    AUTHORITATIVE = "authoritative"
    REPUTABLE = "reputable"
    COMMUNITY = "community"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResearchEvidence:
    source_id: str
    url: str
    title: str
    claim: str
    published_at: str | None = None
    tier: SourceTier = SourceTier.UNKNOWN
    confidence: float = 0.5

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.claim.strip():
            raise ValueError("source_id and claim are required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class Contradiction:
    left: ResearchEvidence
    right: ResearchEvidence
    reason: str


@dataclass(frozen=True)
class ResearchSynthesis:
    evidence: tuple[ResearchEvidence, ...]
    contradictions: tuple[Contradiction, ...]
    citation_map: dict[str, str]
    confidence: float


class ResearchIntelligenceRuntime:
    """Evidence ledger for multi-source research with citation preservation."""

    _negations = frozenset({"not", "never", "no", "without", "false", "incorrect", "failed"})
    _primary_hosts = (
        ".gov",
        ".edu",
        "docs.python.org",
        "learn.microsoft.com",
        "developer.mozilla.org",
        "github.com",
    )

    @classmethod
    def infer_tier(cls, url: str) -> SourceTier:
        host = (urlparse(url).hostname or "").casefold()
        if not host:
            return SourceTier.UNKNOWN
        if any(host.endswith(item) or host == item.lstrip(".") for item in cls._primary_hosts):
            return SourceTier.PRIMARY
        if host.endswith(("reuters.com", "apnews.com", "nature.com", "science.org")):
            return SourceTier.REPUTABLE
        if host.endswith(("reddit.com", "stackoverflow.com", "medium.com")):
            return SourceTier.COMMUNITY
        return SourceTier.UNKNOWN

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", text.casefold())
            if len(token) > 2 and token not in {"the", "and", "for", "with", "that", "this"}
        }

    @classmethod
    def _polarity(cls, text: str) -> int:
        tokens = set(re.findall(r"[a-z]+", text.casefold()))
        return -1 if tokens & cls._negations else 1

    @classmethod
    def contradictory(cls, left: ResearchEvidence, right: ResearchEvidence) -> bool:
        left_tokens = cls._tokens(left.claim)
        right_tokens = cls._tokens(right.claim)
        if not left_tokens or not right_tokens:
            return False
        overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
        return overlap >= 0.55 and cls._polarity(left.claim) != cls._polarity(right.claim)

    def synthesize(self, evidence: tuple[ResearchEvidence, ...]) -> ResearchSynthesis:
        if not evidence:
            return ResearchSynthesis((), (), {}, 0.0)
        normalized: list[ResearchEvidence] = []
        seen: set[tuple[str, str]] = set()
        for item in evidence:
            key = (item.url, " ".join(item.claim.casefold().split()))
            if key in seen:
                continue
            seen.add(key)
            tier = item.tier if item.tier != SourceTier.UNKNOWN else self.infer_tier(item.url)
            normalized.append(
                ResearchEvidence(
                    item.source_id,
                    item.url,
                    item.title,
                    item.claim,
                    item.published_at,
                    tier,
                    item.confidence,
                )
            )
        contradictions: list[Contradiction] = []
        for index, left in enumerate(normalized):
            for right in normalized[index + 1 :]:
                if self.contradictory(left, right):
                    contradictions.append(Contradiction(left, right, "overlapping claim with opposite polarity"))
        weights = {
            SourceTier.PRIMARY: 1.0,
            SourceTier.AUTHORITATIVE: 0.9,
            SourceTier.REPUTABLE: 0.82,
            SourceTier.COMMUNITY: 0.58,
            SourceTier.UNKNOWN: 0.45,
        }
        total_weight = sum(weights[item.tier] for item in normalized)
        confidence = (
            sum(item.confidence * weights[item.tier] for item in normalized) / total_weight
            if total_weight
            else 0.0
        )
        if contradictions:
            confidence *= max(0.35, 1 - min(0.5, len(contradictions) * 0.08))
        return ResearchSynthesis(
            tuple(normalized),
            tuple(contradictions),
            {item.source_id: item.url for item in normalized},
            max(0.0, min(1.0, confidence)),
        )
