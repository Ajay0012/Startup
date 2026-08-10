from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from math import sqrt
from pathlib import Path

import numpy as np


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


@dataclass(frozen=True)
class SpeakerEmbeddingHealth:
    available: bool
    model_path: str
    dimension: int | None
    normalized_error: str | None = None


class SherpaSpeakerEmbeddingProvider:
    """Lazy local speaker embedding extraction using PANGU's sherpa-onnx dependency."""

    def __init__(
        self,
        model_path: Path,
        *,
        num_threads: int = 1,
        provider: str = "cpu",
        minimum_seconds: float = 1.0,
        maximum_seconds: float = 20.0,
    ) -> None:
        self.model_path = model_path
        self.num_threads = max(1, min(8, num_threads))
        self.provider = provider
        self.minimum_seconds = minimum_seconds
        self.maximum_seconds = maximum_seconds
        self._extractor: object | None = None
        self._lock = threading.Lock()
        self._load_error: str | None = None

    def _load(self) -> object:
        if self._extractor is not None:
            return self._extractor
        if not self.model_path.is_file():
            self._load_error = "SPEAKER_MODEL_UNAVAILABLE"
            raise RuntimeError(self._load_error)
        try:
            import sherpa_onnx
        except ImportError as error:
            self._load_error = "SHERPA_ONNX_UNAVAILABLE"
            raise RuntimeError(self._load_error) from error
        with self._lock:
            if self._extractor is None:
                try:
                    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                        model=str(self.model_path),
                        num_threads=self.num_threads,
                        debug=False,
                        provider=self.provider,
                    )
                    if not config.validate():
                        raise RuntimeError("SPEAKER_MODEL_CONFIG_INVALID")
                    self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
                except (RuntimeError, ValueError, OSError) as error:
                    self._load_error = f"SPEAKER_MODEL_LOAD_FAILED:{type(error).__name__}"
                    raise RuntimeError(self._load_error) from error
        return self._extractor

    def health(self) -> SpeakerEmbeddingHealth:
        if self._extractor is not None:
            dimension = int(getattr(self._extractor, "dim", 0)) or None
            return SpeakerEmbeddingHealth(True, str(self.model_path), dimension)
        if not self.model_path.is_file():
            return SpeakerEmbeddingHealth(
                False,
                str(self.model_path),
                None,
                "SPEAKER_MODEL_UNAVAILABLE",
            )
        if self._load_error:
            return SpeakerEmbeddingHealth(False, str(self.model_path), None, self._load_error)
        return SpeakerEmbeddingHealth(True, str(self.model_path), None)

    def extract(self, samples: tuple[float, ...], sample_rate: int) -> tuple[float, ...]:
        if sample_rate < 8_000 or sample_rate > 192_000:
            raise ValueError("unsupported speaker sample rate")
        duration = len(samples) / sample_rate
        if duration < self.minimum_seconds:
            raise ValueError("speaker sample is too short")
        if duration > self.maximum_seconds:
            samples = samples[: int(sample_rate * self.maximum_seconds)]
        extractor = self._load()
        waveform = np.asarray(samples, dtype=np.float32)
        waveform = np.clip(waveform, -1.0, 1.0)
        stream = extractor.create_stream()  # type: ignore[attr-defined]
        stream.accept_waveform(sample_rate=sample_rate, waveform=waveform)
        stream.input_finished()
        if not extractor.is_ready(stream):  # type: ignore[attr-defined]
            raise RuntimeError("SPEAKER_AUDIO_NOT_READY")
        embedding = extractor.compute(stream)  # type: ignore[attr-defined]
        values = tuple(float(value) for value in embedding)
        if len(values) < 8:
            raise RuntimeError("SPEAKER_EMBEDDING_INVALID")
        return values


class SpeakerIdentityRuntime:
    """Deterministic speaker-embedding matcher with explicit enrollment policy."""

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
            score = sum(
                left * right for left, right in zip(profile.embedding, candidate, strict=True)
            )
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
