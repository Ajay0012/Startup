from __future__ import annotations

from importlib import import_module
from math import isfinite
from pathlib import Path
from time import perf_counter
from typing import Any

from .voice import AudioFrame, TranscriptionResult, VoiceOutcome


class FasterWhisperTranscriptionProvider:
    """Lazy local Faster Whisper provider with truthful unavailable states."""

    def __init__(
        self,
        model_path: Path,
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 5,
    ) -> None:
        self.model_path = model_path.resolve()
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self._model: Any | None = None
        self._status = "NOT_LOADED"
        self._last_error: str | None = None

    def _load(self) -> bool:
        if self._model is not None:
            return True
        if not self.model_path.is_dir():
            self._status = "MISSING"
            self._last_error = "WHISPER_MODEL_UNAVAILABLE"
            return False
        try:
            module = import_module("faster_whisper")
            model_type = getattr(module, "WhisperModel")
            self._model = model_type(
                str(self.model_path),
                device=self.device,
                compute_type=self.compute_type,
                local_files_only=True,
            )
        except (ImportError, ModuleNotFoundError, AttributeError):
            self._status = "BACKEND_UNAVAILABLE"
            self._last_error = "WHISPER_BACKEND_UNAVAILABLE"
            return False
        except (OSError, RuntimeError, ValueError):
            self._status = "LOAD_FAILED"
            self._last_error = "WHISPER_MODEL_LOAD_FAILED"
            return False
        self._status = "AVAILABLE"
        self._last_error = None
        return True

    @staticmethod
    def _flatten(frames: tuple[AudioFrame, ...]) -> tuple[float, ...]:
        return tuple(sample for frame in frames for sample in frame.samples)

    def transcribe(self, frames: tuple[AudioFrame, ...]) -> TranscriptionResult:
        if not frames:
            return TranscriptionResult(
                "",
                "",
                verification_state="UNVERIFIED",
                normalized_error="WHISPER_EMPTY_AUDIO",
            )
        if not self._load():
            return TranscriptionResult(
                "",
                "",
                verification_state="UNAVAILABLE",
                normalized_error=self._last_error,
            )

        samples = self._flatten(frames)
        if not samples or any(not isfinite(sample) for sample in samples):
            return TranscriptionResult(
                "",
                "",
                verification_state="UNVERIFIED",
                normalized_error="WHISPER_INVALID_AUDIO",
            )

        try:
            numpy = import_module("numpy")
            audio = numpy.asarray(samples, dtype=numpy.float32)
            started = perf_counter()
            segments_iter, info = self._model.transcribe(
                audio,
                beam_size=self.beam_size,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            segments = tuple(segments_iter)
            latency_ms = (perf_counter() - started) * 1000
            text = " ".join(str(segment.text).strip() for segment in segments).strip()
            public_segments = tuple(
                {
                    "start": float(segment.start),
                    "end": float(segment.end),
                    "text": str(segment.text).strip(),
                }
                for segment in segments
            )
            language = str(getattr(info, "language", "unknown") or "unknown")
            probability = float(getattr(info, "language_probability", 0.0) or 0.0)
            duration = float(getattr(info, "duration", len(samples) / 16000) or 0.0)
            return TranscriptionResult(
                text,
                text,
                detected_language=language,
                language_probability=probability,
                segments=public_segments,
                duration=duration,
                model_profile=f"{self.device}-{self.compute_type}",
                inference_latency_ms=latency_ms,
                verification_state="VERIFIED" if text else "UNVERIFIED",
                normalized_error=None if text else "WHISPER_NO_SPEECH",
            )
        except (OSError, RuntimeError, ValueError, TypeError):
            self._last_error = "WHISPER_INFERENCE_FAILED"
            return TranscriptionResult(
                "",
                "",
                verification_state="UNVERIFIED",
                normalized_error=self._last_error,
            )

    def diagnostics(self) -> dict[str, object]:
        return {
            "provider": "faster-whisper",
            "status": self._status,
            "model_path_sanitized": self.model_path.name,
            "device": self.device,
            "compute_type": self.compute_type,
            "beam_size": self.beam_size,
            "last_error": self._last_error,
        }


class TranscriptionWakePhraseVerifier:
    """Fail-closed wake phrase confirmation using the local STT provider."""

    def __init__(self, transcriber: FasterWhisperTranscriptionProvider) -> None:
        self.transcriber = transcriber

    @staticmethod
    def _normalise(value: str) -> str:
        return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())

    def verify(self, frames: tuple[AudioFrame, ...], phrase: str) -> VoiceOutcome:
        result = self.transcriber.transcribe(frames)
        if result.verification_state == "UNAVAILABLE":
            return VoiceOutcome.UNAVAILABLE
        if result.normalized_error is not None and not result.normalized_transcript:
            return VoiceOutcome.UNCERTAIN
        transcript = self._normalise(result.normalized_transcript)
        expected = self._normalise(phrase)
        if not transcript or not expected:
            return VoiceOutcome.UNCERTAIN
        return VoiceOutcome.CONFIRMED if expected in transcript else VoiceOutcome.REJECTED
