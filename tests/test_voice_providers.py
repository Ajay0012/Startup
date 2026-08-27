from pathlib import Path

from pangu.voice import AudioFrame, VoiceOutcome
from pangu.voice_providers import (
    FasterWhisperTranscriptionProvider,
    TranscriptionWakePhraseVerifier,
)


def test_missing_whisper_model_fails_closed(tmp_path: Path) -> None:
    provider = FasterWhisperTranscriptionProvider(tmp_path / "missing-model")
    result = provider.transcribe((AudioFrame((0.1,) * 512, 0.0, 0),))

    assert result.verification_state == "UNAVAILABLE"
    assert result.normalized_error == "WHISPER_MODEL_UNAVAILABLE"
    assert result.normalized_transcript == ""


def test_empty_audio_is_not_reported_as_success(tmp_path: Path) -> None:
    provider = FasterWhisperTranscriptionProvider(tmp_path / "unused-model")
    result = provider.transcribe(())

    assert result.verification_state == "UNVERIFIED"
    assert result.normalized_error == "WHISPER_EMPTY_AUDIO"


def test_wake_verifier_propagates_unavailable_state(tmp_path: Path) -> None:
    provider = FasterWhisperTranscriptionProvider(tmp_path / "missing-model")
    verifier = TranscriptionWakePhraseVerifier(provider)

    assert verifier.verify((AudioFrame((0.1,) * 512, 0.0, 0),), "Pangu") == VoiceOutcome.UNAVAILABLE


def test_runtime_builder_does_not_wire_fake_transcription(tmp_path: Path) -> None:
    from pangu.runtime_builder import RuntimeBuilder

    container = RuntimeBuilder(tmp_path).build()

    assert isinstance(container.voice.transcriber, FasterWhisperTranscriptionProvider)
    assert isinstance(container.voice.verifier, TranscriptionWakePhraseVerifier)
