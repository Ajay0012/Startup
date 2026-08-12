from pathlib import Path

import numpy as np
import pytest

from pangu.personalized_wake import (
    PersonalizedWakeWordVerifier,
    acoustic_features,
    build_profile_payload,
    distance_to_score,
    dtw_distance,
    speech_regions,
)


def synthetic_phrase(frequency: float = 220.0) -> np.ndarray:
    sample_rate = 16000
    silence = np.zeros(int(0.25 * sample_rate), dtype=np.float32)
    t1 = np.arange(int(0.55 * sample_rate), dtype=np.float32) / sample_rate
    t2 = np.arange(int(0.50 * sample_rate), dtype=np.float32) / sample_rate
    first = 0.25 * np.sin(2 * np.pi * frequency * t1)
    second = 0.22 * np.sin(2 * np.pi * (frequency * 1.55) * t2)
    gap = np.zeros(int(0.10 * sample_rate), dtype=np.float32)
    return np.concatenate((silence, first, gap, second, silence)).astype(np.float32)


def test_acoustic_features_are_finite_and_channel_normalized() -> None:
    features = acoustic_features(synthetic_phrase())
    assert features.ndim == 2
    assert features.shape[1] == 48
    assert features.shape[0] > 50
    assert np.isfinite(features).all()


def test_speech_regions_find_phrase_but_reject_silence() -> None:
    assert speech_regions(np.zeros(32000, dtype=np.float32), 16000) == ()
    regions = speech_regions(synthetic_phrase(), 16000)
    assert regions
    start, end = regions[0]
    assert start < end
    assert 0.45 <= (end - start) / 16000 <= 3.2


def test_dtw_prefers_same_pronunciation_over_different_signal() -> None:
    enrolled = acoustic_features(synthetic_phrase(220.0))
    same = acoustic_features(synthetic_phrase(222.0))
    different = acoustic_features(synthetic_phrase(510.0))
    same_score = distance_to_score(dtw_distance(enrolled, same))
    different_score = distance_to_score(dtw_distance(enrolled, different))
    assert same_score > different_score


def test_profile_requires_multiple_samples_and_never_claims_raw_audio() -> None:
    template = acoustic_features(synthetic_phrase())
    embedding = np.linspace(0.1, 1.0, 16, dtype=np.float32)
    with pytest.raises(ValueError):
        build_profile_payload((template,), (embedding,))
    payload = build_profile_payload(
        (template, template, template, template),
        (embedding, embedding, embedding, embedding),
    )
    assert payload["phrase"] == "Hey Pangu"
    assert payload["raw_audio_persisted"] is False
    assert payload["enrollment_count"] == 4
    assert "speaker_embedding" in payload
    assert "templates" in payload


def test_health_requires_enrollment_before_owner_only_wake(tmp_path: Path) -> None:
    verifier = PersonalizedWakeWordVerifier(
        tmp_path / "owner_wake_profile.json",
        tmp_path / "speaker.onnx",
    )
    health = verifier.health()
    assert health.available is False
    assert health.normalized_error == "OWNER_WAKE_ENROLLMENT_REQUIRED"
