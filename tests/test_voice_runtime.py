import asyncio
import json
import wave
from pathlib import Path

import pytest

from pangu.events import EventBus
from pangu.language import LanguageRuntime
from pangu.voice import (
    AmbientNoiseEstimator,
    AudioDevice,
    AudioFrame,
    BoundedWaveDecoder,
    DeviceDisconnectedError,
    FakeAudioInputAdapter,
    FakeTranscriptionProvider,
    FakeVad,
    FakeVoiceActivityDetector,
    FakeWakePhraseVerifier,
    FakeWakeWordEngine,
    SherpaOnnxSileroVadAdapter,
    SileroVadModelManifest,
    SpeechSegment,
    SpeechSegmentController,
    SpeechTerminationReason,
    UnavailableVoiceActivityDetector,
    VadActivationService,
    VadConfiguration,
    VadDecisionSource,
    VadDetectorResetError,
    VadFileInferenceService,
    VadModelStatus,
    VoiceActivityResult,
    VoiceActivityState,
    VoiceCaptureRequest,
    VoiceConfig,
    VoiceSessionRuntime,
    VoiceState,
)

FORBIDDEN_AUDIO_KEYS = {"samples", "raw_audio", "audio_samples", "pcm", "waveform"}


class ExactSherpaNative:
    """Fake with the public sherpa-onnx VAD method signatures, exactly."""

    def __init__(self, speech: bool = False, type_error: bool = False) -> None:
        self.speech = speech
        self.type_error = type_error
        self.accepted: list[list[float]] = []
        self.speech_calls = 0
        self.flush_calls = 0
        self.reset_calls = 0
        self.queued_segments = ["native-only"]

    def accept_waveform(self, samples):
        if self.type_error:
            raise TypeError("native argument mismatch")
        self.accepted.append(samples)

    def is_speech_detected(self):
        self.speech_calls += 1
        return self.speech

    def flush(self):
        self.flush_calls += 1

    def reset(self):
        self.reset_calls += 1


def sherpa_adapter(native: ExactSherpaNative) -> SherpaOnnxSileroVadAdapter:
    manifest = SileroVadModelManifest(
        "silero-vad-v4",
        "sherpa-onnx",
        "v4",
        "silero_vad.onnx",
        "0" * 64,
        "https://example.test/vad",
    )
    adapter = SherpaOnnxSileroVadAdapter(manifest, Path("silero_vad.onnx"))
    adapter._detector = native
    adapter.initialized = True
    return adapter


def native_result(
    native: ExactSherpaNative, samples: tuple[float, ...] | None = None
) -> VoiceActivityResult:
    return sherpa_adapter(native).analyze(samples or (0.2,) * 512, 16000, 0.0)


def window_vad(
    speech: bool = True, probability: float = 1.0, energy: float = 0.2
) -> VoiceActivityResult:
    return VoiceActivityResult(0.0, probability, speech, energy, energy >= 0.01, 32.0)


def test_sherpa_native_accept_waveform_receives_samples_only() -> None:
    native = ExactSherpaNative()
    native_result(native)
    assert native.accepted == [[0.2] * 512]


def test_sherpa_native_every_frame_has_exactly_512_samples(tmp_path: Path) -> None:
    path = tmp_path / "windows.wav"
    write_wav(path, 16000, 1, 600)
    native = ExactSherpaNative()
    config = VadConfiguration()
    VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(native),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert native.accepted and all(len(samples) == 512 for samples in native.accepted)


def test_sherpa_wrong_sample_rate_rejected_before_native_entry() -> None:
    native = ExactSherpaNative()
    with pytest.raises(ValueError, match="INVALID_VAD_FRAME"):
        sherpa_adapter(native).analyze((0.2,) * 512, 8000, 0.0)
    assert not native.accepted


def test_sherpa_wrong_frame_length_rejected_before_native_entry() -> None:
    native = ExactSherpaNative()
    with pytest.raises(ValueError, match="INVALID_VAD_FRAME"):
        sherpa_adapter(native).analyze((0.2,) * 511, 16000, 0.0)
    assert not native.accepted


@pytest.mark.parametrize("sample", [float("nan"), float("inf"), -float("inf")])
def test_sherpa_non_finite_samples_rejected(sample: float) -> None:
    native = ExactSherpaNative()
    with pytest.raises(ValueError, match="INVALID_VAD_FRAME"):
        sherpa_adapter(native).analyze((sample,) * 512, 16000, 0.0)
    assert not native.accepted


@pytest.mark.parametrize("sample", [-1.01, 1.01])
def test_sherpa_out_of_range_samples_rejected(sample: float) -> None:
    native = ExactSherpaNative()
    with pytest.raises(ValueError, match="INVALID_VAD_FRAME"):
        sherpa_adapter(native).analyze((sample,) * 512, 16000, 0.0)
    assert not native.accepted


def test_sherpa_is_speech_detected_is_called_as_a_method() -> None:
    native = ExactSherpaNative(True)
    assert native_result(native).is_speech and native.speech_calls == 1


def test_sherpa_never_converts_bound_method_to_bool() -> None:
    native = ExactSherpaNative(False)
    assert not native_result(native).is_speech
    assert native.speech_calls == 1


def test_authoritative_backend_accepts_speech_and_energy_without_probability() -> None:
    activity = native_result(ExactSherpaNative(True))
    assert activity.probability is None
    assert activity.accepted_for_segmentation(0.99)


def test_probability_based_detector_still_requires_threshold() -> None:
    activity = VoiceActivityResult(0, 0.49, True, 0.2, True, 32)
    assert not activity.accepted_for_segmentation(0.5)


def test_sherpa_probability_is_none() -> None:
    assert native_result(ExactSherpaNative()).probability is None


def test_authoritative_file_aggregate_probabilities_are_none(tmp_path: Path) -> None:
    path = tmp_path / "authoritative.wav"
    write_wav(path, 16000, 1, 512)
    native = ExactSherpaNative()
    config = VadConfiguration()
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(native),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.average_vad_probability is None and result.maximum_vad_probability is None


def test_authoritative_decisions_increment_processed_windows(tmp_path: Path) -> None:
    path = tmp_path / "count.wav"
    write_wav(path, 16000, 1, 600)
    native = ExactSherpaNative()
    config = VadConfiguration()
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(native),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.processed_window_count == 2


def test_zero_processed_windows_cannot_be_verified(tmp_path: Path) -> None:
    path = tmp_path / "zero.wav"
    write_wav(path, 16000, 1, 512)

    class NoWindows(FakeVoiceActivityDetector):
        def analyze(self, samples, sample_rate, timestamp):
            raise RuntimeError("no result")

    config = VadConfiguration()
    result = VadFileInferenceService(
        BoundedWaveDecoder(), NoWindows(), lambda: SpeechSegmentController(config), config
    ).infer(path)
    assert result.processed_window_count == 0 and result.verification_state == "UNVERIFIED"


def test_sherpa_flush_is_called_once_at_eof(tmp_path: Path) -> None:
    path = tmp_path / "flush.wav"
    write_wav(path, 16000, 1, 512)
    native = ExactSherpaNative()
    config = VadConfiguration()
    VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(native),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert native.flush_calls == 1


def test_sherpa_reset_occurs_before_and_after_file_inference(tmp_path: Path) -> None:
    path = tmp_path / "reset.wav"
    write_wav(path, 16000, 1, 512)
    native = ExactSherpaNative()
    config = VadConfiguration()
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(native),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert native.reset_calls == 2 and result.detector_reset_before and result.detector_reset_after


def test_sherpa_adapter_is_reusable_for_sequential_file_inferences(tmp_path: Path) -> None:
    path = tmp_path / "reuse.wav"
    write_wav(path, 16000, 1, 512)
    native = ExactSherpaNative()
    config = VadConfiguration()
    service = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(native),
        lambda: SpeechSegmentController(config),
        config,
    )
    assert (
        service.infer(path).verification_state
        == service.infer(path).verification_state
        == "VERIFIED"
    )
    assert native.reset_calls == 4


def test_sherpa_silence_creates_no_accepted_segments(tmp_path: Path) -> None:
    path = tmp_path / "silence-native.wav"
    write_wav(path, 16000, 1, 1024)
    config = VadConfiguration(minimum_speech_ms=32)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(ExactSherpaNative(False)),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.accepted_segment_count == 0


def test_sherpa_speech_can_create_and_accept_segment(tmp_path: Path) -> None:
    path = tmp_path / "speech-native.wav"
    write_wav(path, 16000, 1, 1024)
    config = VadConfiguration(minimum_speech_ms=32)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(ExactSherpaNative(True)),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.accepted_segment_count == 1


def test_native_type_error_is_normalized_without_traceback(tmp_path: Path) -> None:
    path = tmp_path / "type-error.wav"
    write_wav(path, 16000, 1, 512)
    config = VadConfiguration()
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(ExactSherpaNative(type_error=True)),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.normalized_error == "VAD_INFERENCE_FAILED" and "Traceback" not in repr(result)


def test_sherpa_native_queued_segments_are_not_duplicated_into_output(tmp_path: Path) -> None:
    path = tmp_path / "queued.wav"
    write_wav(path, 16000, 1, 1024)
    native = ExactSherpaNative(True)
    config = VadConfiguration(minimum_speech_ms=32)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(native),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.accepted_segment_count == 1 and all(
        "native-only" not in str(item) for item in result.segments
    )


def test_activation_failure_retains_an_unavailable_detector(tmp_path: Path) -> None:
    service = VadActivationService(tmp_path, tmp_path / "missing-manifest.json")
    detector = service.activate()
    assert isinstance(detector, UnavailableVoiceActivityDetector)
    assert service.detector is detector


def test_partial_activation_closes_candidate_native_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "model_id": "silero-vad-v4",
                "backend": "sherpa-onnx",
                "version": "v4",
                "filename": "silero_vad.onnx",
                "sha256": "0" * 64,
                "download_source": "https://example.test/vad",
            }
        ),
        encoding="utf-8",
    )

    class PartiallyActivatedAdapter:
        closed = False

        def __init__(self, manifest, model_path) -> None:
            self.manifest = manifest
            self.model_path = model_path

        def initialize(self):
            return VadModelStatus.LOAD_FAILED

        def close(self) -> None:
            type(self).closed = True

    monkeypatch.setattr("pangu.voice.SherpaOnnxSileroVadAdapter", PartiallyActivatedAdapter)
    service = VadActivationService(tmp_path, manifest_path)
    assert isinstance(service.activate(), UnavailableVoiceActivityDetector)
    assert PartiallyActivatedAdapter.closed


def test_authoritative_candidate_start_increments_count(tmp_path: Path) -> None:
    path = tmp_path / "authoritative-candidate.wav"
    write_wav(path, 16000, 1, 1024)
    config = VadConfiguration(minimum_speech_ms=32)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(ExactSherpaNative(True)),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.speech_candidate_count == 1
    assert result.speech_candidate_count >= result.accepted_segment_count


def test_two_accepted_segments_report_at_least_two_candidates(tmp_path: Path) -> None:
    path = tmp_path / "two-segments.wav"
    write_wav(path, 16000, 1, 3072)
    config = VadConfiguration(minimum_speech_ms=32, minimum_silence_ms=32)
    sequence = (
        window_vad(True, 0.8),
        window_vad(True, 0.8),
        window_vad(False, 0.1),
        window_vad(True, 0.8),
        window_vad(True, 0.8),
        window_vad(False, 0.1),
    )
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        FakeVoiceActivityDetector(sequence),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.accepted_segment_count == 2
    assert result.speech_candidate_count >= 2


def test_resumed_ending_state_does_not_start_a_duplicate_candidate() -> None:
    item = SpeechSegmentController(VadConfiguration(minimum_speech_ms=32, minimum_silence_ms=64))
    item.process(AudioFrame((0.2,) * 512, 0, 0), window_vad(True, 0.8), "session")
    item.process(AudioFrame((0.2,) * 512, 0.032, 1), window_vad(True, 0.8), "session")
    item.process(AudioFrame((0.0,) * 512, 0.064, 2), window_vad(False, 0.1, 0.0), "session")
    item.process(AudioFrame((0.2,) * 512, 0.096, 3), window_vad(True, 0.8), "session")
    assert item.candidate_start_count == 1


def test_rejected_short_candidate_increments_candidate_and_rejected_counts(tmp_path: Path) -> None:
    path = tmp_path / "short-candidate.wav"
    write_wav(path, 16000, 1, 1024)
    config = VadConfiguration(minimum_speech_ms=96)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        FakeVoiceActivityDetector((window_vad(True, 0.8), window_vad(False, 0.1))),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.speech_candidate_count == result.rejected_candidate_count == 1


def test_probability_detector_segment_probability_metadata_is_numeric(tmp_path: Path) -> None:
    path = tmp_path / "probability-segment.wav"
    write_wav(path, 16000, 1, 1024)
    config = VadConfiguration(minimum_speech_ms=32)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        FakeVoiceActivityDetector((window_vad(True, 0.7), window_vad(True, 0.9))),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    segment = result.segments[0]
    assert segment["average_probability"] == pytest.approx(0.8)
    assert segment["maximum_probability"] == 0.9


def test_authoritative_segment_probability_metadata_is_null(tmp_path: Path) -> None:
    path = tmp_path / "null-probability-segment.wav"
    write_wav(path, 16000, 1, 1024)
    config = VadConfiguration(minimum_speech_ms=32)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(ExactSherpaNative(True)),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.segments[0]["average_probability"] is None
    assert result.segments[0]["maximum_probability"] is None


def test_none_probabilities_are_not_fabricated_as_zero(tmp_path: Path) -> None:
    path = tmp_path / "no-fabricated-probability.wav"
    write_wav(path, 16000, 1, 1024)
    config = VadConfiguration(minimum_speech_ms=32)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(ExactSherpaNative(True)),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.average_vad_probability is None
    assert result.segments[0]["average_probability"] is None


def test_segment_audio_duration_matches_retained_sample_count(tmp_path: Path) -> None:
    path = tmp_path / "duration.wav"
    write_wav(path, 16000, 1, 1024)
    config = VadConfiguration(minimum_speech_ms=32)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        FakeVoiceActivityDetector((window_vad(True, 0.8), window_vad(True, 0.8))),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    segment = result.segments[0]
    assert segment["audio_duration_seconds"] == segment["total_samples"] / segment["sample_rate"]
    assert segment["duration_seconds"] == segment["audio_duration_seconds"]


def test_elapsed_duration_is_separate_from_audio_duration() -> None:
    segment = SpeechSegment("s", "id", 1.0, 3.0, 16000, [0.2] * 512)
    metadata = segment.public()
    assert metadata["audio_duration_seconds"] == 512 / 16000
    assert metadata["elapsed_duration_seconds"] == 2.0


def test_inconsistent_segment_metadata_cannot_be_verified() -> None:
    invalid = (
        {
            "total_samples": 512,
            "sample_rate": 16000,
            "audio_duration_seconds": 9.0,
            "duration_seconds": 9.0,
            "average_probability": None,
            "maximum_probability": None,
            "decision_source": VadDecisionSource.AUTHORITATIVE_BACKEND,
        },
    )
    assert not VadFileInferenceService._metadata_is_valid(invalid, 1, 1, 1, 0, 0, None, None, True)


def test_metadata_invariant_failure_returns_typed_unverified_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "invalid-metadata.wav"
    write_wav(path, 16000, 1, 1024)
    original_public = SpeechSegment.public

    def inconsistent_public(segment: SpeechSegment) -> dict[str, object]:
        metadata = original_public(segment)
        metadata["duration_seconds"] = 99.0
        return metadata

    monkeypatch.setattr(SpeechSegment, "public", inconsistent_public)
    config = VadConfiguration(minimum_speech_ms=32)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(ExactSherpaNative(True)),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.verification_state == "UNVERIFIED"
    assert result.normalized_error == "VAD_METADATA_INVALID"


def test_public_file_result_contains_no_raw_samples_after_metadata_repair(tmp_path: Path) -> None:
    path = tmp_path / "public-safe.wav"
    write_wav(path, 16000, 1, 1024)
    config = VadConfiguration(minimum_speech_ms=32)
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        sherpa_adapter(ExactSherpaNative(True)),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert_public_payload_safe(result.public())


def assert_public_payload_safe(value: object) -> None:
    if isinstance(value, dict):
        assert not (FORBIDDEN_AUDIO_KEYS & set(value))
        for child in value.values():
            assert_public_payload_safe(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_public_payload_safe(child)
    else:
        assert not isinstance(value, (bytes, bytearray, memoryview, BaseException))
        assert type(value).__module__.split(".")[0] != "numpy"
        assert not hasattr(value, "read") and not hasattr(value, "accept_waveform")


def write_wav(path: Path, rate: int, channels: int, frames: int) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((b"\x00\x40" * channels) * frames)


def test_bounded_wave_decoder_downmixes_resamples_and_hides_samples(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    write_wav(path, 44100, 2, 441)
    result = BoundedWaveDecoder().decode(path)
    assert result.audio and result.audio.metadata.normalized_sample_rate == 16000
    assert result.audio.metadata.normalized_sample_count == 160
    assert "samples" not in result.audio.public()
    result.audio.clear_samples()
    assert result.audio.samples_cleared and not result.audio.samples


def test_file_inference_uses_padded_windows_and_cleans_audio(tmp_path: Path) -> None:
    path = tmp_path / "short.wav"
    write_wav(path, 16000, 1, 600)
    config = VadConfiguration(minimum_speech_ms=100)
    detector = FakeVoiceActivityDetector()
    service = VadFileInferenceService(
        BoundedWaveDecoder(), detector, lambda: SpeechSegmentController(config), config
    )
    result = service.infer(path)
    assert result.verification_state == "VERIFIED"
    assert result.processed_window_count == 2 and result.padded_sample_count == 424
    assert result.decoded_samples_cleared and result.segment_samples_cleared
    assert_public_payload_safe(result.public())


def test_file_result_allows_safe_segment_metadata_and_no_raw_audio(tmp_path: Path) -> None:
    path = tmp_path / "speech.wav"
    write_wav(path, 16000, 1, 2048)
    config = VadConfiguration(minimum_speech_ms=100)
    detector = FakeVoiceActivityDetector(
        tuple(VoiceActivityResult(0.0, 1.0, True, 0.2, True, 32.0) for _ in range(4))
    )
    result = VadFileInferenceService(
        BoundedWaveDecoder(), detector, lambda: SpeechSegmentController(config), config
    ).infer(path)
    assert result.segments and result.segment_samples_cleared and result.decoded_samples_cleared
    assert_public_payload_safe(result.public())


def test_file_inference_no_speech_and_duration_excludes_padding(tmp_path: Path) -> None:
    path = tmp_path / "silence.wav"
    write_wav(path, 16000, 1, 600)
    config = VadConfiguration()
    result = VadFileInferenceService(
        BoundedWaveDecoder(),
        FakeVoiceActivityDetector(),
        lambda: SpeechSegmentController(config),
        config,
    ).infer(path)
    assert result.verification_state == "VERIFIED" and not result.segments
    assert result.padded_sample_count == 424 and result.normalized_duration_seconds == 600 / 16000


def test_detector_failure_is_normalized_and_audio_is_cleared(tmp_path: Path) -> None:
    class FailingDetector(FakeVoiceActivityDetector):
        def analyze(self, samples, sample_rate, timestamp):
            raise RuntimeError("private native failure")

    path = tmp_path / "failure.wav"
    write_wav(path, 16000, 1, 512)
    config = VadConfiguration()
    result = VadFileInferenceService(
        BoundedWaveDecoder(), FailingDetector(), lambda: SpeechSegmentController(config), config
    ).infer(path)
    assert result.normalized_error == "VAD_INFERENCE_FAILED" and result.decoded_samples_cleared


def test_detector_reset_failure_is_unverified(tmp_path: Path) -> None:
    class ResetFailure(FakeVoiceActivityDetector):
        def __init__(self) -> None:
            super().__init__()
            self.reset_count = 0

        def reset(self):
            self.reset_count += 1
            if self.reset_count > 1:
                raise VadDetectorResetError()

    path = tmp_path / "reset.wav"
    write_wav(path, 16000, 1, 512)
    config = VadConfiguration()
    result = VadFileInferenceService(
        BoundedWaveDecoder(), ResetFailure(), lambda: SpeechSegmentController(config), config
    ).infer(path)
    assert result.normalized_error == "VAD_CLEANUP_UNVERIFIED" and not result.cleanup_verified


def test_repeated_file_inference_replays_fake_detector_state(tmp_path: Path) -> None:
    path = tmp_path / "repeat.wav"
    write_wav(path, 16000, 1, 512)
    config = VadConfiguration()
    service = VadFileInferenceService(
        BoundedWaveDecoder(),
        FakeVoiceActivityDetector((vad(False, 0.2),)),
        lambda: SpeechSegmentController(config),
        config,
    )
    first, second = service.infer(path), service.infer(path)
    assert first.verification_state == second.verification_state == "VERIFIED"
    assert first.average_vad_probability == second.average_vad_probability == 0.2


def vad(speech: bool = True, probability: float = 1.0, energy: float = 0.2) -> VoiceActivityResult:
    return VoiceActivityResult(0.0, probability, speech, energy, energy >= 0.01, 100.0)


def frame(sequence: int, value: float = 0.2) -> AudioFrame:
    return AudioFrame((value,) * 1600, sequence / 10, sequence)


def controller(**kwargs: object) -> SpeechSegmentController:
    return SpeechSegmentController(
        VadConfiguration(minimum_speech_ms=200, minimum_silence_ms=300, **kwargs)
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("speech_threshold", 0.0),
        ("speech_threshold", float("nan")),
        ("speech_threshold", float("inf")),
        ("minimum_speech_ms", 0),
        ("maximum_utterance_seconds", 0),
        ("minimum_energy_floor", float("nan")),
        ("minimum_energy_floor", float("inf")),
    ],
)
def test_vad_configuration_rejects_invalid_values(field, value) -> None:
    with pytest.raises(ValueError):
        VadConfiguration(**{field: value})


def test_fake_detector_reset_replays_results() -> None:
    from pangu.voice import FakeVoiceActivityDetector

    item = vad()
    fake = FakeVoiceActivityDetector((item,))
    assert fake.analyze((), 16000, 0) == item
    fake.reset()
    assert fake.analyze((), 16000, 0) == item


def test_calibration_is_bounded_and_safe() -> None:
    estimator = AmbientNoiseEstimator(3)
    profile = estimator.profile("fake", tuple(frame(i, 0.02) for i in range(10)), 0.01)
    assert profile.verification_state == "VERIFIED"
    assert profile.recommended_energy_gate >= 0.01 and profile.percentile_95_rms > 0
    assert estimator.retained_count == 3
    estimator.reset()
    assert estimator.retained_count == 0


def test_calibration_rejects_malformed_and_contaminated() -> None:
    estimator = AmbientNoiseEstimator()
    bad = estimator.profile("fake", (AudioFrame((float("nan"),), 0),), 0.01)
    assert bad.verification_state == "UNVERIFIED"
    loud = estimator.profile("fake", tuple(frame(i, 0.9) for i in range(3)), 0.01)
    assert loud.normalized_error == "SPEECH_CONTAMINATION"


@pytest.mark.parametrize(
    "activity",
    [vad(True, 0.49), vad(True, 1, 0.001), vad(False, 1, 0.2), vad(True, float("nan"), 0.2)],
)
def test_invalid_vad_combinations_do_not_start_speech(activity) -> None:
    item = controller()
    item.process(frame(0), activity, "s")
    assert item.state == VoiceActivityState.IDLE


def test_segment_prefix_pause_trailing_and_privacy() -> None:
    item = controller(prefix_padding_ms=200, trailing_padding_ms=100)
    item.process(frame(0, 0.001), vad(False, 0, 0.001), "s")
    item.process(frame(1), vad(), "s")
    item.process(frame(2), vad(), "s")
    assert item.state == VoiceActivityState.SPEAKING
    item.process(frame(3, 0.001), vad(False, 0, 0.001), "s")
    assert item.state == VoiceActivityState.ENDING
    item.process(frame(4), vad(), "s")
    assert item.state == VoiceActivityState.SPEAKING
    item.process(frame(5, 0.001), vad(False, 0, 0.001), "s")
    item.process(frame(6, 0.001), vad(False, 0, 0.001), "s")
    result = item.process(frame(7, 0.001), vad(False, 0, 0.001), "s")
    assert result and result.termination_reason == SpeechTerminationReason.END_SILENCE
    assert result.prefix_duration_ms == 100 and result.trailing_duration_ms == 100
    assert "samples" not in result.public() and "0.2" not in repr(result)
    result.clear_samples()
    assert not result.samples and result.public()["total_samples"] == 0


def test_short_candidate_and_terminal_paths_are_reusable() -> None:
    item = controller()
    item.process(frame(0), vad(), "one")
    item.process(frame(1, 0.001), vad(False, 0, 0.001), "one")
    assert item.state == VoiceActivityState.IDLE
    item.process(frame(2), vad(), "two")
    item.process(frame(3), vad(), "two")
    result = item.cancel()
    assert result and result.termination_reason == SpeechTerminationReason.CANCELLED
    assert item.state == VoiceActivityState.IDLE
    item.process(frame(4), vad(), "three")
    item.process(frame(5), vad(), "three")
    assert (
        item.device_disconnected().termination_reason == SpeechTerminationReason.DEVICE_DISCONNECTED
    )


def test_maximum_duration_is_hard_bound() -> None:
    item = SpeechSegmentController(
        VadConfiguration(minimum_speech_ms=100, maximum_utterance_seconds=1)
    )
    result = None
    for sequence in range(20):
        result = item.process(frame(sequence), vad(), "s") or result
    assert result and result.termination_reason == SpeechTerminationReason.MAXIMUM_DURATION
    assert len(result.samples) <= 16000 and item.state == VoiceActivityState.IDLE


def test_fake_voice_runtime_start_stop() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        voice = VoiceSessionRuntime(
            FakeAudioInputAdapter(),
            FakeVad(),
            FakeWakeWordEngine(),
            FakeWakePhraseVerifier(),
            FakeTranscriptionProvider(),
            events,
            LanguageRuntime(),
        )
        await voice.start()
        assert voice.state == VoiceState.IDLE_LISTENING
        await voice.stop()
        assert voice.state == VoiceState.STOPPED
        await events.stop()

    asyncio.run(run())


def test_missing_microphone_fails_closed() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        voice = VoiceSessionRuntime(
            FakeAudioInputAdapter([]),
            FakeVad(),
            FakeWakeWordEngine(),
            FakeWakePhraseVerifier(),
            FakeTranscriptionProvider(),
            events,
            LanguageRuntime(),
        )
        await voice.start()
        assert voice.state == VoiceState.DEVICE_UNAVAILABLE
        await voice.stop()
        await events.stop()

    asyncio.run(run())


def test_illegal_transition_is_rejected() -> None:
    voice = VoiceSessionRuntime(
        FakeAudioInputAdapter([AudioDevice("default", "mic", True, 1)]),
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        EventBus(),
        LanguageRuntime(),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(voice._transition(VoiceState.COMMAND_LISTENING, "invalid"))


class FramesAdapter(FakeAudioInputAdapter):
    def start(self, selector, on_frame=None):
        if on_frame:
            for _ in range(4):
                on_frame((0.1,) * 16000, 1, 16000, False)


class DisconnectingAdapter(FakeAudioInputAdapter):
    def start(self, selector, on_frame=None):
        raise DeviceDisconnectedError()


def build_voice(adapter, events):
    return VoiceSessionRuntime(
        adapter,
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        events,
        LanguageRuntime(),
    )


def test_capture_uses_fake_frames_and_cleans_up() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        voice = VoiceSessionRuntime(
            FramesAdapter(),
            FakeVad(),
            FakeWakeWordEngine(),
            FakeWakePhraseVerifier(),
            FakeTranscriptionProvider(),
            events,
            LanguageRuntime(),
        )
        result = await voice.capture_test(VoiceCaptureRequest(1))
        assert result.verification_state == "VERIFIED"
        assert result.worker_stopped and result.cleanup_verified
        assert result.total_samples > 0 and result.rms_level is not None
        await events.stop()

    asyncio.run(run())


def test_capture_cancelled_is_unverified() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        voice = VoiceSessionRuntime(
            FakeAudioInputAdapter(),
            FakeVad(),
            FakeWakeWordEngine(),
            FakeWakePhraseVerifier(),
            FakeTranscriptionProvider(),
            events,
            LanguageRuntime(),
        )
        task = asyncio.create_task(voice.capture_test(VoiceCaptureRequest(2)))
        await asyncio.sleep(0)
        task.cancel()
        result = await task
        assert result.normalized_error == "CANCELLED"
        assert result.verification_state == "UNVERIFIED"
        await events.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "selector,expected", [(None, "VERIFIED"), ("default", "VERIFIED"), ("stale", "UNVERIFIED")]
)
def test_device_selection(selector, expected) -> None:
    voice = VoiceSessionRuntime(
        FakeAudioInputAdapter(),
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        EventBus(),
        LanguageRuntime(),
    )
    assert voice.select_device(selector).verification_state == expected


def test_ambiguous_devices_fail_closed() -> None:
    voice = VoiceSessionRuntime(
        FakeAudioInputAdapter(
            [AudioDevice("a", "same", False, 1), AudioDevice("b", "same", False, 1)]
        ),
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        EventBus(),
        LanguageRuntime(),
    )
    assert voice.select_device().normalized_error == "AMBIGUOUS_DEVICE"


def test_callback_ignored_after_stop() -> None:
    voice = VoiceSessionRuntime(
        FakeAudioInputAdapter(),
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        EventBus(),
        LanguageRuntime(),
    )
    voice.accept_callback_frame((0.1,), 1, 16000)
    assert voice.metrics["frames_received"] == 0


def test_resampler_downmixes_stereo() -> None:
    voice = VoiceSessionRuntime(
        FakeAudioInputAdapter(),
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        EventBus(),
        LanguageRuntime(),
        VoiceConfig(),
    )
    frame = voice.resampler.normalize((1.0, -1.0, 0.5, 0.5), 2, 16000, 1, 1)
    assert frame and frame.samples == (0.0, 0.5)


def test_device_disconnection_returns_typed_failure() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        result = await build_voice(DisconnectingAdapter(), events).capture_test(
            VoiceCaptureRequest(1)
        )
        assert result.normalized_error == "DEVICE_DISCONNECTED" and result.retryable
        await events.stop()

    asyncio.run(run())


def test_device_disconnection_cleans_worker_and_buffer() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        voice = build_voice(DisconnectingAdapter(), events)
        result = await voice.capture_test(VoiceCaptureRequest(1))
        assert result.worker_stopped and result.ring_buffer_cleared
        await events.stop()

    asyncio.run(run())


def test_capture_started_and_completed_events_are_emitted() -> None:
    async def run() -> None:
        events = EventBus()
        seen = []

        async def collect(event):
            seen.append(event)

        events.subscribe("voice.capture.started", collect)
        events.subscribe("voice.capture.completed", collect)
        await events.start()
        await build_voice(FramesAdapter(), events).capture_test(VoiceCaptureRequest(1))
        await asyncio.sleep(0.01)
        assert {event.event_type for event in seen} == {
            "voice.capture.started",
            "voice.capture.completed",
        }
        await events.stop()

    asyncio.run(run())


def test_capture_cancelled_event_is_emitted() -> None:
    async def run() -> None:
        events = EventBus()
        seen = []

        async def collect(event):
            seen.append(event.event_type)

        events.subscribe("voice.capture.cancelled", collect)
        await events.start()
        voice = build_voice(FakeAudioInputAdapter(), events)
        task = asyncio.create_task(voice.capture_test(VoiceCaptureRequest(2)))
        await asyncio.sleep(0)
        task.cancel()
        await task
        await asyncio.sleep(0.01)
        assert "voice.capture.cancelled" in seen
        await events.stop()

    asyncio.run(run())


def test_capture_event_payload_contains_no_raw_audio() -> None:
    async def run() -> None:
        events = EventBus()
        payloads = []

        async def collect(event):
            payloads.append(event.payload)

        events.subscribe("voice.capture.completed", collect)
        await events.start()
        await build_voice(FramesAdapter(), events).capture_test(VoiceCaptureRequest(1))
        await asyncio.sleep(0.01)
        assert all(
            not {"samples", "raw_audio", "stream", "thread"} & set(payload) for payload in payloads
        )
        await events.stop()

    asyncio.run(run())
