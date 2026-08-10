from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import sqrt


class SpeakerRole(StrEnum):
    OWNER = "owner"
    GUEST = "guest"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SpeakerProfile:
    profile_id: str
    role: SpeakerRole
    embedding: tuple[float, ...]
    minimum_similarity: float = 0.78

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("profile_id is required")
        if len(self.embedding) < 8:
            raise ValueError("speaker embedding is too small")
        if not 0.5 <= self.minimum_similarity <= 0.99:
            raise ValueError("minimum_similarity must be between 0.5 and 0.99")


@dataclass(frozen=True)
class SpeakerMatch:
    role: SpeakerRole
    profile_id: str | None
    similarity: float
    accepted: bool


class SpeakerIdentityRuntime:
    """Deterministic speaker-embedding matcher.

    Audio feature extraction is supplied by an explicit local speaker-embedding model;
    this runtime owns enrollment/matching policy only and never treats wake-word
    detection as identity proof.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, SpeakerProfile] = {}

    @staticmethod
    def _normalize(vector: tuple[float, ...]) -> tuple[float, ...]:
        norm = sqrt(sum(value * value for value in vector))
        if norm <= 1e-12:
            raise ValueError("speaker embedding has zero norm")
        return tuple(value / norm for value in vector)

    def enroll(self, profile: SpeakerProfile) -> None:
        normalized = SpeakerProfile(
            profile.profile_id,
            profile.role,
            self._normalize(profile.embedding),
            profile.minimum_similarity,
        )
        self._profiles[normalized.profile_id] = normalized

    def revoke(self, profile_id: str) -> bool:
        return self._profiles.pop(profile_id, None) is not None

    def identify(self, embedding: tuple[float, ...]) -> SpeakerMatch:
        if not self._profiles:
            return SpeakerMatch(SpeakerRole.UNKNOWN, None, 0.0, False)
        candidate = self._normalize(embedding)
        best: SpeakerProfile | None = None
        best_score = -1.0
        for profile in self._profiles.values():
            if len(profile.embedding) != len(candidate):
                continue
            score = sum(left * right for left, right in zip(profile.embedding, candidate, strict=True))
            if score > best_score:
                best = profile
                best_score = score
        if best is None or best_score < best.minimum_similarity:
            return SpeakerMatch(SpeakerRole.UNKNOWN, None, max(0.0, best_score), False)
        return SpeakerMatch(best.role, best.profile_id, min(1.0, best_score), True)


@dataclass(frozen=True)
class TrustContext:
    speaker: SpeakerRole
    windows_session_unlocked: bool
    trusted_device: bool
    local_presence: bool
    recent_strong_auth: bool = False


@dataclass(frozen=True)
class TrustDecision:
    score: float
    privileged_allowed: bool
    confirmation_required: bool
    reasons: tuple[str, ...]


class IdentityTrustEngine:
    """Combine speaker, device and session context for permission decisions."""

    def assess(self, context: TrustContext, *, consequential: bool = False) -> TrustDecision:
        score = 0.0
        reasons: list[str] = []
        if context.speaker == SpeakerRole.OWNER:
            score += 0.45
            reasons.append("owner speaker match")
        elif context.speaker == SpeakerRole.GUEST:
            score += 0.12
            reasons.append("guest speaker")
        if context.windows_session_unlocked:
            score += 0.18
            reasons.append("unlocked Windows session")
        if context.trusted_device:
            score += 0.16
            reasons.append("trusted device")
        if context.local_presence:
            score += 0.11
            reasons.append("local presence")
        if context.recent_strong_auth:
            score += 0.18
            reasons.append("recent strong authentication")
        score = min(1.0, score)
        privileged = context.speaker == SpeakerRole.OWNER and score >= 0.72
        confirmation = consequential and (not privileged or not context.recent_strong_auth)
        return TrustDecision(score, privileged, confirmation, tuple(reasons))
