from __future__ import annotations

import json
from dataclasses import dataclass
from math import exp
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np

from .speaker_identity import SherpaSpeakerEmbeddingProvider


@dataclass(frozen=True)
class PersonalizedWakeMatch:
    score: float
    speaker_similarity: float
    start_sample: int
    end_sample: int


@dataclass(frozen=True)
class PersonalizedWakeHealth:
    available: bool
    profile_path: str
    speaker_model_path: str
    normalized_error: str | None = None


def _hz_to_mel(hz: np.ndarray | float) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + np.asarray(hz, dtype=np.float64) / 700.0)


def _mel_to_hz(mel: np.ndarray | float) -> np.ndarray:
    return 700.0 * (np.power(10.0, np.asarray(mel, dtype=np.float64) / 2595.0) - 1.0)


def _mel_filterbank(sample_rate: int, n_fft: int, n_mels: int = 24) -> np.ndarray:
    low_mel = float(_hz_to_mel(80.0))
    high_mel = float(_hz_to_mel(min(7600.0, sample_rate / 2.0 - 50.0)))
    mel_points = np.linspace(low_mel, high_mel, n_mels + 2)
    hz_points = _mel_to_hz(mel_points)
    bins = np.floor((n_fft + 1) * hz_points / sample_rate).astype(int)
    bins = np.clip(bins, 0, n_fft // 2)
    filters = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for index in range(n_mels):
        left, center, right = int(bins[index]), int(bins[index + 1]), int(bins[index + 2])
        if center <= left:
            center = min(left + 1, n_fft // 2)
        if right <= center:
            right = min(center + 1, n_fft // 2)
        if center > left:
            filters[index, left:center] = np.linspace(0.0, 1.0, center - left, endpoint=False)
        if right > center:
            filters[index, center:right] = np.linspace(1.0, 0.0, right - center, endpoint=False)
    return filters


def _frame_signal(samples: np.ndarray, sample_rate: int) -> tuple[np.ndarray, int]:
    frame_size = max(1, int(round(0.025 * sample_rate)))
    hop = max(1, int(round(0.010 * sample_rate)))
    if samples.size < frame_size:
        samples = np.pad(samples, (0, frame_size - samples.size))
    frame_count = 1 + max(0, (samples.size - frame_size) // hop)
    starts = np.arange(frame_count) * hop
    frames = np.stack([samples[start : start + frame_size] for start in starts], axis=0)
    return frames, hop


def speech_regions(samples: np.ndarray, sample_rate: int) -> tuple[tuple[int, int], ...]:
    """Return robust speech-like regions while rejecting stationary wind/background energy."""
    if samples.size == 0:
        return ()
    waveform = np.asarray(samples, dtype=np.float32)
    frames, hop = _frame_signal(waveform, sample_rate)
    rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)
    floor = float(np.percentile(rms, 25))
    upper = float(np.percentile(rms, 90))
    threshold = max(0.006, floor * 2.2, floor + 0.16 * max(0.0, upper - floor))
    active = rms >= threshold

    # Bridge gaps shorter than 180 ms so "Hey Pangu" remains one region.
    max_gap = max(1, round(0.18 / 0.010))
    active_indices = np.flatnonzero(active)
    if active_indices.size:
        for left, right in zip(active_indices[:-1], active_indices[1:], strict=False):
            if 1 < right - left <= max_gap + 1:
                active[left : right + 1] = True

    regions: list[tuple[int, int]] = []
    start: int | None = None
    for index, is_active in enumerate(active):
        if is_active and start is None:
            start = index
        if start is not None and (not is_active or index == len(active) - 1):
            end_frame = index if is_active and index == len(active) - 1 else index - 1
            duration = (end_frame - start + 1) * 0.010 + 0.015
            if 0.45 <= duration <= 3.2:
                pad = int(round(0.10 * sample_rate))
                begin_sample = max(0, start * hop - pad)
                end_sample = min(
                    waveform.size,
                    end_frame * hop + int(round(0.025 * sample_rate)) + pad,
                )
                regions.append((begin_sample, end_sample))
            start = None
    return tuple(regions)


def acoustic_features(samples: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Noise-robust log-mel + delta representation for personalized phrase matching."""
    waveform = np.asarray(samples, dtype=np.float32).reshape(-1)
    if waveform.size == 0:
        raise ValueError("wake sample is empty")
    waveform = waveform - float(np.mean(waveform))
    peak = float(np.max(np.abs(waveform)))
    if peak > 1e-6:
        waveform = waveform / max(peak, 0.08)
    # Pre-emphasis suppresses low-frequency wind/handling rumble.
    emphasized = np.empty_like(waveform)
    emphasized[0] = waveform[0]
    emphasized[1:] = waveform[1:] - 0.97 * waveform[:-1]
    frames, _ = _frame_signal(emphasized, sample_rate)
    window = np.hamming(frames.shape[1]).astype(np.float32)
    n_fft = 512
    spectrum = np.fft.rfft(frames * window, n=n_fft, axis=1)
    power = (np.abs(spectrum) ** 2).astype(np.float32)
    filters = _mel_filterbank(sample_rate, n_fft)
    mel_energy = np.maximum(power @ filters.T, 1e-10)
    log_mel = np.log(mel_energy)
    # Cepstral mean/variance normalization removes channel and steady-noise coloration.
    mean = np.mean(log_mel, axis=0, keepdims=True)
    std = np.std(log_mel, axis=0, keepdims=True)
    normalized = (log_mel - mean) / np.maximum(std, 0.25)
    delta = np.zeros_like(normalized)
    if normalized.shape[0] >= 3:
        delta[1:-1] = (normalized[2:] - normalized[:-2]) * 0.5
        delta[0] = normalized[1] - normalized[0]
        delta[-1] = normalized[-1] - normalized[-2]
    return np.concatenate((normalized, delta), axis=1).astype(np.float32)


def dtw_distance(left: np.ndarray, right: np.ndarray) -> float:
    if left.ndim != 2 or right.ndim != 2 or left.shape[1] != right.shape[1]:
        raise ValueError("wake feature dimensions do not match")
    if left.shape[0] == 0 or right.shape[0] == 0:
        return float("inf")
    # Sakoe-Chiba band keeps matching bounded while allowing accent/rate variation.
    n, m = left.shape[0], right.shape[0]
    band = max(abs(n - m) + 2, int(max(n, m) * 0.24))
    previous = np.full(m + 1, np.inf, dtype=np.float64)
    previous[0] = 0.0
    for i in range(1, n + 1):
        current = np.full(m + 1, np.inf, dtype=np.float64)
        start = max(1, i - band)
        stop = min(m, i + band)
        for j in range(start, stop + 1):
            diff = left[i - 1] - right[j - 1]
            cost = float(np.sqrt(np.mean(diff * diff)))
            current[j] = cost + min(current[j - 1], previous[j], previous[j - 1])
        previous = current
    return float(previous[m] / max(n, m))


def distance_to_score(distance: float) -> float:
    if not np.isfinite(distance):
        return 0.0
    return float(exp(-max(0.0, distance) / 0.58))


def recommended_keyword_threshold(templates: tuple[np.ndarray, ...]) -> float:
    if len(templates) < 2:
        return 0.58
    scores = [
        distance_to_score(dtw_distance(templates[i], templates[j]))
        for i in range(len(templates))
        for j in range(i + 1, len(templates))
    ]
    # Fail conservatively: enrollment variability may lower genuine scores, but never
    # allow an automatically learned threshold below 0.50.
    return float(min(0.82, max(0.50, median(scores) - 0.12)))


def normalize_embedding(values: np.ndarray) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-8:
        raise ValueError("speaker embedding has zero norm")
    return vector / norm


class PersonalizedWakeWordVerifier:
    """Owner-specific wake gate: acoustic phrase template + neural speaker verification."""

    def __init__(self, profile_path: Path, speaker_model_path: Path) -> None:
        self.profile_path = profile_path
        self.speaker_model_path = speaker_model_path
        self._profile: dict[str, Any] | None = None
        self._templates: tuple[np.ndarray, ...] = ()
        self._speaker_provider: SherpaSpeakerEmbeddingProvider | None = None

    def health(self) -> PersonalizedWakeHealth:
        if not self.profile_path.is_file():
            return PersonalizedWakeHealth(
                False,
                str(self.profile_path),
                str(self.speaker_model_path),
                "OWNER_WAKE_ENROLLMENT_REQUIRED",
            )
        if not self.speaker_model_path.is_file():
            return PersonalizedWakeHealth(
                False,
                str(self.profile_path),
                str(self.speaker_model_path),
                "SPEAKER_MODEL_UNAVAILABLE",
            )
        try:
            self._load_profile()
        except (ValueError, OSError, json.JSONDecodeError):
            return PersonalizedWakeHealth(
                False,
                str(self.profile_path),
                str(self.speaker_model_path),
                "OWNER_WAKE_PROFILE_INVALID",
            )
        return PersonalizedWakeHealth(True, str(self.profile_path), str(self.speaker_model_path))

    def _load_profile(self) -> None:
        if self._profile is not None:
            return
        data = json.loads(self.profile_path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or data.get("sample_rate") != 16000:
            raise ValueError("unsupported personalized wake profile")
        raw_templates = data.get("templates")
        speaker_embedding = data.get("speaker_embedding")
        if not isinstance(raw_templates, list) or len(raw_templates) < 4:
            raise ValueError("at least four wake templates are required")
        if not isinstance(speaker_embedding, list) or len(speaker_embedding) < 8:
            raise ValueError("speaker embedding is required")
        templates = tuple(np.asarray(item, dtype=np.float32) for item in raw_templates)
        feature_dim = templates[0].shape[1] if templates[0].ndim == 2 else 0
        if feature_dim <= 0 or any(
            item.ndim != 2 or item.shape[1] != feature_dim for item in templates
        ):
            raise ValueError("wake templates are malformed")
        self._templates = templates
        self._profile = data
        self._speaker_provider = SherpaSpeakerEmbeddingProvider(self.speaker_model_path)

    def verify(self, samples: np.ndarray, sample_rate: int = 16000) -> PersonalizedWakeMatch | None:
        self._load_profile()
        assert self._profile is not None
        assert self._speaker_provider is not None
        waveform = np.asarray(samples, dtype=np.float32).reshape(-1)
        regions = speech_regions(waveform, sample_rate)
        if not regions:
            return None
        keyword_threshold = float(self._profile.get("keyword_threshold", 0.58))
        speaker_threshold = float(self._profile.get("speaker_similarity_threshold", 0.78))
        owner_embedding = normalize_embedding(
            np.asarray(self._profile["speaker_embedding"], dtype=np.float32)
        )
        best: PersonalizedWakeMatch | None = None
        for start, end in regions:
            candidate = waveform[start:end]
            duration = candidate.size / sample_rate
            if not 0.65 <= duration <= 3.0:
                continue
            features = acoustic_features(candidate, sample_rate)
            template_scores = sorted(
                (
                    distance_to_score(dtw_distance(features, template))
                    for template in self._templates
                ),
                reverse=True,
            )
            # Require agreement from more than one enrollment example to reject accidental matches.
            keyword_score = float(np.mean(template_scores[: min(3, len(template_scores))]))
            if keyword_score < keyword_threshold:
                continue
            try:
                candidate_embedding = normalize_embedding(
                    np.asarray(
                        self._speaker_provider.extract(
                            tuple(float(x) for x in candidate), sample_rate
                        ),
                        dtype=np.float32,
                    )
                )
            except (RuntimeError, ValueError):
                continue
            if candidate_embedding.shape != owner_embedding.shape:
                continue
            similarity = float(np.dot(candidate_embedding, owner_embedding))
            if similarity < speaker_threshold:
                continue
            match = PersonalizedWakeMatch(keyword_score, similarity, start, end)
            if best is None or (match.score + match.speaker_similarity) > (
                best.score + best.speaker_similarity
            ):
                best = match
        return best


def build_profile_payload(
    templates: tuple[np.ndarray, ...],
    speaker_embeddings: tuple[np.ndarray, ...],
    *,
    phrase: str = "Hey Pangu",
    speaker_similarity_threshold: float = 0.78,
) -> dict[str, object]:
    if len(templates) < 4 or len(speaker_embeddings) < 4:
        raise ValueError("at least four successful enrollment samples are required")
    normalized_speakers = np.stack([normalize_embedding(item) for item in speaker_embeddings])
    centroid = normalize_embedding(np.mean(normalized_speakers, axis=0))
    genuine_similarities = normalized_speakers @ centroid
    learned_speaker_threshold = max(
        0.72,
        min(speaker_similarity_threshold, float(np.percentile(genuine_similarities, 10)) - 0.04),
    )
    return {
        "version": 1,
        "phrase": phrase,
        "sample_rate": 16000,
        "keyword_threshold": recommended_keyword_threshold(templates),
        "speaker_similarity_threshold": learned_speaker_threshold,
        "templates": [item.tolist() for item in templates],
        "speaker_embedding": centroid.tolist(),
        "enrollment_count": len(templates),
        "raw_audio_persisted": False,
    }
