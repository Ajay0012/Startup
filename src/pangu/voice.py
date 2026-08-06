"""Local, bounded input-side voice runtime; no recording or speech output."""

from __future__ import annotations

import asyncio
import hashlib
import json
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from importlib import import_module
from math import isfinite, sqrt
from queue import Full, Queue
from pathlib import Path
from threading import Event, Thread
from typing import Any, ClassVar, Protocol, cast
from uuid import uuid4

from .events import EventBus, EventEnvelope
from .language import LanguageRuntime


class VoiceState(StrEnum):
    STOPPED = "STOPPED"
    INITIALIZING = "INITIALIZING"
    IDLE_LISTENING = "IDLE_LISTENING"
    SPEECH_CANDIDATE = "SPEECH_CANDIDATE"
    WAKE_CANDIDATE = "WAKE_CANDIDATE"
    WAKE_CONFIRMED = "WAKE_CONFIRMED"
    COMMAND_LISTENING = "COMMAND_LISTENING"
    TURN_ENDING = "TURN_ENDING"
    TRANSCRIBING = "TRANSCRIBING"
    COMMAND_READY = "COMMAND_READY"
    COOLDOWN = "COOLDOWN"
    DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"
    FAILED = "FAILED"
    SHUTTING_DOWN = "SHUTTING_DOWN"


class VoiceOutcome(StrEnum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"


class VoiceActivityState(StrEnum):
    """States owned by the deterministic, input-side segment controller."""

    IDLE = "IDLE"
    CANDIDATE = "CANDIDATE"
    SPEAKING = "SPEAKING"
    ENDING = "ENDING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    # Compatibility names for callers which used the original scaffold.
    IDLE_LISTENING = "IDLE"
    SPEECH_CANDIDATE = "CANDIDATE"
    USER_SPEAKING = "SPEAKING"
    TURN_ENDING = "ENDING"


@dataclass(frozen=True)
class VadConfiguration:
    sample_rate: int = 16000
    window_size: int = 512
    speech_threshold: float = 0.5
    minimum_speech_ms: int = 250
    minimum_silence_ms: int = 700
    prefix_padding_ms: int = 400
    trailing_padding_ms: int = 200
    maximum_utterance_seconds: int = 30
    minimum_energy_floor: float = 0.01
    calibration_duration_seconds: int = 10

    def __post_init__(self) -> None:
        if (
            self.sample_rate != 16000
            or self.window_size <= 0
            or not isfinite(self.speech_threshold)
            or not 0 < self.speech_threshold <= 1
            or self.minimum_speech_ms < 1
            or self.minimum_silence_ms < 1
            or self.prefix_padding_ms < 0
            or self.trailing_padding_ms < 0
            or not 1 <= self.maximum_utterance_seconds <= 300
            or not isfinite(self.minimum_energy_floor)
            or not 0 <= self.minimum_energy_floor <= 1
            or not 3 <= self.calibration_duration_seconds <= 30
        ):
            raise ValueError("invalid VAD configuration")


@dataclass(frozen=True)
class VoiceActivityResult:
    timestamp: float
    probability: float
    is_speech: bool
    energy_level: float
    energy_gate_passed: bool
    frame_duration_ms: float
    detector_name: str = "fake-vad"
    verification_state: str = "VERIFIED"
    normalized_error: str | None = None

    def valid(self) -> bool:
        return (
            isfinite(self.timestamp)
            and isfinite(self.probability)
            and 0 <= self.probability <= 1
            and isfinite(self.energy_level)
            and self.energy_level >= 0
            and isfinite(self.frame_duration_ms)
            and self.frame_duration_ms > 0
            and self.normalized_error is None
        )


class VoiceActivityDetector(Protocol):
    def analyze(
        self, samples: tuple[float, ...], sample_rate: int, timestamp: float
    ) -> VoiceActivityResult: ...
    def reset(self) -> None: ...


class FakeVoiceActivityDetector:
    def __init__(self, results: tuple[VoiceActivityResult, ...] = ()) -> None:
        self._configured = tuple(results)
        self._results = deque(results)

    def analyze(
        self, samples: tuple[float, ...], sample_rate: int, timestamp: float
    ) -> VoiceActivityResult:
        return (
            self._results.popleft()
            if self._results
            else VoiceActivityResult(
                timestamp, 0.0, False, 0.0, False, len(samples) * 1000 / sample_rate
            )
        )

    def reset(self) -> None:
        self._results = deque(self._configured)


class SpeechTerminationReason(StrEnum):
    END_SILENCE = "END_SILENCE"
    MAXIMUM_DURATION = "MAXIMUM_DURATION"
    CANCELLED = "CANCELLED"
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"
    RUNTIME_SHUTDOWN = "RUNTIME_SHUTDOWN"
    VAD_FAILURE = "VAD_FAILURE"
    END_OF_FILE = "END_OF_FILE"


@dataclass
class SpeechSegment:
    session_id: str
    segment_id: str
    start_timestamp: float
    end_timestamp: float
    sample_rate: int
    _samples: list[float] = field(repr=False)
    speech_frame_count: int = 0
    silence_frame_count: int = 0
    probabilities: list[float] = field(default_factory=list)
    termination_reason: SpeechTerminationReason | None = None
    prefix_duration_ms: float = 0.0
    trailing_duration_ms: float = 0.0

    @property
    def samples(self) -> tuple[float, ...]:
        """Read-only immediate-consumer view; never suitable for serialization."""
        return tuple(self._samples)

    def public(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "segment_id": self.segment_id,
            "duration_seconds": self.end_timestamp - self.start_timestamp,
            "sample_rate": self.sample_rate,
            "total_samples": len(self._samples),
            "speech_frame_count": self.speech_frame_count,
            "silence_frame_count": self.silence_frame_count,
            "termination_reason": self.termination_reason,
            "average_probability": sum(self.probabilities) / len(self.probabilities)
            if self.probabilities
            else 0.0,
            "maximum_probability": max(self.probabilities, default=0.0),
            "prefix_duration_ms": self.prefix_duration_ms,
            "trailing_duration_ms": self.trailing_duration_ms,
            "verification_state": "VERIFIED",
        }

    def clear_samples(self) -> None:
        self._samples.clear()

    def clear(self) -> None:
        self.clear_samples()
        self.probabilities.clear()


class SpeechSegmentController:
    """Bounded VAD+energy segmenter.  It deliberately knows nothing about devices or models."""

    _allowed: ClassVar[dict[VoiceActivityState, frozenset[VoiceActivityState]]] = {
        VoiceActivityState.IDLE: frozenset({VoiceActivityState.CANDIDATE}),
        VoiceActivityState.CANDIDATE: frozenset(
            {VoiceActivityState.IDLE, VoiceActivityState.SPEAKING}
        ),
        VoiceActivityState.SPEAKING: frozenset({VoiceActivityState.ENDING}),
        VoiceActivityState.ENDING: frozenset(
            {VoiceActivityState.SPEAKING, VoiceActivityState.IDLE}
        ),
        VoiceActivityState.CANCELLED: frozenset({VoiceActivityState.IDLE}),
        VoiceActivityState.FAILED: frozenset({VoiceActivityState.IDLE}),
    }

    def __init__(
        self,
        config: VadConfiguration,
        gate: float | None = None,
        profile: AmbientNoiseProfile | None = None,
        detector: VoiceActivityDetector | None = None,
    ) -> None:
        self.config = config
        proposed = (
            profile.recommended_energy_gate
            if profile and profile.verification_state == "VERIFIED"
            else gate
        )
        self.gate = max(config.minimum_energy_floor, proposed or config.minimum_energy_floor)
        self.detector = detector
        self.state = VoiceActivityState.IDLE
        self._prefix: deque[AudioFrame] = deque()
        self._candidate: list[tuple[AudioFrame, VoiceActivityResult]] = []
        self._segment: SpeechSegment | None = None
        self._silence_ms = 0.0
        self._trailing: list[AudioFrame] = []
        self.events: list[dict[str, object]] = []

    @property
    def maximum_retained_samples(self) -> int:
        return self.config.sample_rate * self.config.maximum_utterance_seconds + (
            self.config.sample_rate
            * (self.config.prefix_padding_ms + self.config.trailing_padding_ms)
            // 1000
        )

    def _duration(self, frame: AudioFrame) -> float:
        return len(frame.samples) * 1000 / self.config.sample_rate

    def _valid_frame(self, frame: AudioFrame, activity: VoiceActivityResult) -> bool:
        return (
            bool(frame.samples)
            and len(frame.samples)
            <= self.config.sample_rate * self.config.maximum_utterance_seconds
            and all(isfinite(x) for x in frame.samples)
            and activity.valid()
            and abs(self._duration(frame) - activity.frame_duration_ms) < 1.0
        )

    def _speech(self, frame: AudioFrame, activity: VoiceActivityResult) -> bool:
        return (
            self._valid_frame(frame, activity)
            and activity.is_speech
            and activity.energy_gate_passed
            and activity.energy_level >= self.gate
            and activity.probability >= self.config.speech_threshold
        )

    def _emit(self, event_type: str, **payload: object) -> None:
        # Metadata only: frames, samples, native errors, and detector objects never cross this boundary.
        self.events.append({"event_type": event_type, **payload})

    def _transition(self, next_state: VoiceActivityState) -> None:
        if next_state not in self._allowed[self.state]:
            raise RuntimeError("illegal speech segment transition")
        self.state = next_state

    def _append_prefix(self, frame: AudioFrame) -> None:
        self._prefix.append(frame)
        while (
            sum(len(item.samples) for item in self._prefix) * 1000 / self.config.sample_rate
            > self.config.prefix_padding_ms
        ):
            self._prefix.popleft()

    def _finish(self, reason: SpeechTerminationReason) -> SpeechSegment | None:
        segment = self._segment
        if segment is None:
            self.reset()
            return None
        segment.termination_reason = reason
        self._emit("voice.speech.stopped", **segment.public())
        self.reset()
        return segment

    def process(
        self, frame: AudioFrame, activity: VoiceActivityResult, session_id: str
    ) -> SpeechSegment | None:
        if self.state in {VoiceActivityState.CANCELLED, VoiceActivityState.FAILED}:
            raise RuntimeError("controller must be reset before reuse")
        valid = self._speech(frame, activity)
        if not self._valid_frame(frame, activity):
            self._emit("voice.vad.error", normalized_error="MALFORMED_FRAME")
            valid = False
        if self.state == VoiceActivityState.IDLE:
            if valid:
                self._transition(VoiceActivityState.CANDIDATE)
                self._candidate = [(frame, activity)]
                self._emit("voice.speech.candidate", session_id=session_id)
            else:
                self._append_prefix(frame)
            return None
        if self.state == VoiceActivityState.CANDIDATE:
            if not valid:
                self._candidate.clear()
                self._transition(VoiceActivityState.IDLE)
                self._emit("voice.speech.rejected", normalized_error="INSUFFICIENT_SPEECH")
                self._append_prefix(frame)
                return None
            self._candidate.append((frame, activity))
            speech_ms = sum(self._duration(item[0]) for item in self._candidate)
            if speech_ms < self.config.minimum_speech_ms:
                return None
            candidate_frames = [item[0] for item in self._candidate]
            prefix = list(self._prefix)
            samples = [value for item in prefix + candidate_frames for value in item.samples]
            first = (prefix or candidate_frames)[0]
            self._segment = SpeechSegment(
                session_id,
                str(uuid4()),
                first.timestamp,
                frame.timestamp,
                self.config.sample_rate,
                samples,
                len(candidate_frames),
                0,
                [item[1].probability for item in self._candidate],
                None,
                sum(self._duration(item) for item in prefix),
                0.0,
            )
            self._candidate.clear()
            self._prefix.clear()
            self._transition(VoiceActivityState.SPEAKING)
            self._emit("voice.speech.started", **self._segment.public())
            return None
        assert self._segment is not None
        if valid:
            if self.state == VoiceActivityState.ENDING:
                self._trailing.clear()
                self._silence_ms = 0.0
                self._transition(VoiceActivityState.SPEAKING)
            self._segment._samples.extend(frame.samples)
            self._segment.end_timestamp = frame.timestamp
            self._segment.speech_frame_count += 1
            self._segment.probabilities.append(activity.probability)
        else:
            if self.state == VoiceActivityState.SPEAKING:
                self._transition(VoiceActivityState.ENDING)
            self._trailing.append(frame)
            self._silence_ms += self._duration(frame)
            trailing_limit = self.config.sample_rate * self.config.trailing_padding_ms // 1000
            while sum(len(item.samples) for item in self._trailing) > trailing_limit:
                self._trailing.pop(0)
            if self._silence_ms >= self.config.minimum_silence_ms:
                for item in self._trailing:
                    self._segment._samples.extend(item.samples)
                self._segment.trailing_duration_ms = sum(
                    self._duration(item) for item in self._trailing
                )
                self._segment.silence_frame_count += len(self._trailing)
                self._segment.end_timestamp = frame.timestamp
                return self._finish(SpeechTerminationReason.END_SILENCE)
        if (
            len(self._segment._samples)
            >= self.config.sample_rate * self.config.maximum_utterance_seconds
        ):
            self._segment._samples = self._segment._samples[
                : self.config.sample_rate * self.config.maximum_utterance_seconds
            ]
            return self._finish(SpeechTerminationReason.MAXIMUM_DURATION)
        return None

    def _terminal(self, reason: SpeechTerminationReason) -> SpeechSegment | None:
        active = self._segment is not None
        self.state = (
            VoiceActivityState.CANCELLED
            if reason == SpeechTerminationReason.CANCELLED
            else VoiceActivityState.FAILED
        )
        result = self._finish(reason) if active else None
        if not active:
            self._candidate.clear()
            self._prefix.clear()
            self._trailing.clear()
            self._emit("voice.speech.rejected", normalized_error=reason)
            self.reset()
        return result

    def cancel(self) -> SpeechSegment | None:
        return self._terminal(SpeechTerminationReason.CANCELLED)

    def device_disconnected(self) -> SpeechSegment | None:
        return self._terminal(SpeechTerminationReason.DEVICE_DISCONNECTED)

    def runtime_shutdown(self) -> SpeechSegment | None:
        return self._terminal(SpeechTerminationReason.RUNTIME_SHUTDOWN)

    def vad_failure(self) -> SpeechSegment | None:
        self._emit("voice.vad.error", normalized_error="VAD_FAILURE")
        return self._terminal(SpeechTerminationReason.VAD_FAILURE)

    def end_of_file(self) -> SpeechSegment | None:
        """Terminate a file-backed segment without treating EOF as a device failure."""
        return self._finish(SpeechTerminationReason.END_OF_FILE)

    def calibrate(
        self,
        estimator: AmbientNoiseEstimator,
        device_selector: str,
        frames: tuple[AudioFrame, ...],
        requested_duration_seconds: float,
    ) -> AmbientNoiseProfile:
        self._emit("voice.calibration.started", device_selector=device_selector)
        profile = estimator.profile(
            device_selector,
            frames,
            self.config.minimum_energy_floor,
            requested_duration_seconds,
            self.config.sample_rate,
        )
        self._emit(
            "voice.calibration.completed"
            if profile.verification_state == "VERIFIED"
            else "voice.calibration.failed",
            device_selector=device_selector,
            verification_state=profile.verification_state,
            normalized_error=profile.normalized_error,
        )
        if profile.verification_state == "VERIFIED":
            self.gate = max(self.config.minimum_energy_floor, profile.recommended_energy_gate)
        return profile

    def reset(self) -> None:
        self.state = VoiceActivityState.IDLE
        self._candidate.clear()
        self._prefix.clear()
        self._trailing.clear()
        self._segment = None
        self._silence_ms = 0.0
        if self.detector:
            self.detector.reset()


@dataclass(frozen=True)
class AmbientNoiseProfile:
    device_selector: str
    requested_duration_seconds: float
    observed_duration_seconds: float
    frame_count: int
    total_samples: int
    mean_rms: float
    median_rms: float
    percentile_90_rms: float
    percentile_95_rms: float
    maximum_peak: float
    estimated_noise_floor: float
    recommended_energy_gate: float
    speech_contamination_ratio: float
    confidence: float
    calibration_timestamp: datetime
    verification_state: str
    normalized_error: str | None = None
    retryable: bool = False

    # Earlier scaffold names remain read-only conveniences.
    @property
    def duration_seconds(self) -> float:
        return self.observed_duration_seconds

    @property
    def rms_mean(self) -> float:
        return self.mean_rms

    @property
    def rms_p95(self) -> float:
        return self.percentile_95_rms

    @property
    def peak_level(self) -> float:
        return self.maximum_peak

    @property
    def timestamp(self) -> datetime:
        return self.calibration_timestamp


class AmbientNoiseEstimator:
    """A bounded reservoir (default 256 RMS values), not raw calibration audio."""

    def __init__(self, reservoir_capacity: int = 256) -> None:
        self.reservoir_capacity = reservoir_capacity
        self._values: deque[float] = deque(maxlen=reservoir_capacity)

    @property
    def retained_count(self) -> int:
        return len(self._values)

    def reset(self) -> None:
        self._values.clear()

    def profile(
        self,
        device_selector: str,
        frames: tuple[AudioFrame, ...],
        minimum_gate: float,
        requested_duration_seconds: float | None = None,
        sample_rate: int = 16000,
    ) -> AmbientNoiseProfile:
        self.reset()
        total = 0
        peak = 0.0
        malformed = False
        contamination = 0
        for frame in frames:
            if not frame.samples or not all(isfinite(value) for value in frame.samples):
                malformed = True
                continue
            rms = sqrt(sum(value * value for value in frame.samples) / len(frame.samples))
            self._values.append(rms)
            total += len(frame.samples)
            peak = max(peak, max(abs(x) for x in frame.samples))
            contamination += int(rms >= max(minimum_gate * 3, 0.1))
        values = sorted(self._values)
        observed = total / sample_rate
        requested = (
            requested_duration_seconds if requested_duration_seconds is not None else observed
        )
        error = (
            "MALFORMED_SAMPLES"
            if malformed
            else "NO_USABLE_FRAMES"
            if not values
            else "INSUFFICIENT_FRAMES"
            if len(values) < 3
            else "INSUFFICIENT_DURATION"
            if observed < min(requested, 0.1)
            else "SPEECH_CONTAMINATION"
            if contamination / max(len(values), 1) > 0.2
            else None
        )

        def percentile(p: float) -> float:
            return values[min(len(values) - 1, int(p * (len(values) - 1)))] if values else 0.0

        p95 = percentile(0.95)
        return AmbientNoiseProfile(
            device_selector,
            requested,
            observed,
            len(values),
            total,
            sum(values) / len(values) if values else 0.0,
            percentile(0.5),
            percentile(0.9),
            p95,
            peak,
            min(values) if values else 0.0,
            max(minimum_gate, p95 * 1.5),
            contamination / max(len(values), 1),
            1.0 if error is None else 0.0,
            datetime.now(UTC),
            "VERIFIED" if error is None else "UNVERIFIED",
            error,
            error is not None,
        )


class DeviceDisconnectedError(RuntimeError):
    """Sanitized adapter signal; no native exception text crosses the boundary."""


@dataclass(frozen=True)
class VoiceConfig:
    wake_phrase: str = "Hey Pangu"
    sample_rate: int = 16000
    window_size: int = 512
    minimum_speech_ms: int = 250
    end_silence_ms: int = 700
    prefix_padding_ms: int = 400
    maximum_command_seconds: int = 30
    vad_threshold: float = 0.5
    profile: str = "cpu-balanced"
    audio_queue_capacity_ms: int = 500
    maximum_verified_drop_ratio: float = 0.01


@dataclass(frozen=True)
class AudioDevice:
    selector: str
    name: str
    is_default: bool
    channels: int
    sample_rates: tuple[int, ...] = (16000,)
    default_sample_rate: int = 16000
    availability: str = "AVAILABLE"


@dataclass(frozen=True)
class AudioFrame:
    samples: tuple[float, ...]
    timestamp: float
    sequence: int = 0


class LocalAudioResampler:
    """Deterministic mono linear resampling used off the PortAudio callback."""

    def normalize(
        self,
        samples: tuple[float, ...],
        channels: int,
        source_rate: int,
        timestamp: float,
        sequence: int,
    ) -> AudioFrame | None:
        if channels < 1 or source_rate < 1 or not all(isfinite(x) for x in samples):
            return None
        mono = tuple(
            sum(samples[i : i + channels]) / channels
            for i in range(0, len(samples), channels)
            if len(samples[i : i + channels]) == channels
        )
        if source_rate == 16000:
            return AudioFrame(mono, timestamp, sequence)
        count = round(len(mono) * 16000 / source_rate)
        if not mono or count < 1:
            return None
        output = tuple(mono[min(int(i * source_rate / 16000), len(mono) - 1)] for i in range(count))
        return AudioFrame(output, timestamp, sequence)


class AudioRingBuffer:
    def __init__(self, capacity_ms: int = 3000) -> None:
        self.capacity_ms, self._frames = capacity_ms, deque[AudioFrame]()

    def append(self, frame: AudioFrame) -> None:
        self._frames.append(frame)
        while self.duration_ms() > self.capacity_ms:
            self._frames.popleft()

    def duration_ms(self) -> int:
        return sum(len(x.samples) for x in self._frames) * 1000 // 16000

    def recent(self, milliseconds: int) -> tuple[AudioFrame, ...]:
        frames = list(self._frames)
        total = 0
        selected = []
        for frame in reversed(frames):
            selected.append(frame)
            total += len(frame.samples) * 1000 // 16000
            if total >= milliseconds:
                break
        return tuple(reversed(selected))

    def clear(self) -> None:
        self._frames.clear()


@dataclass(frozen=True)
class WakeDetection:
    keyword: str
    normalized_keyword: str
    start_timestamp: float
    detection_timestamp: float
    score: float
    threshold: float
    engine_name: str
    audio_session_id: str


@dataclass(frozen=True)
class TranscriptionResult:
    original_transcript: str
    normalized_transcript: str
    detected_language: str = "unknown"
    language_probability: float = 0.0
    segments: tuple[dict[str, object], ...] = ()
    duration: float = 0.0
    no_speech_probability: float = 0.0
    model_profile: str = "cpu-balanced"
    inference_latency_ms: float = 0.0
    verification_state: str = "VERIFIED"
    normalized_error: str | None = None


@dataclass(frozen=True)
class VoiceConsentRecord:
    consent_id: str
    profile_id: str
    granted: bool
    recorded_at: str


@dataclass(frozen=True)
class VoiceProfileMetadata:
    profile_id: str
    provider: str
    consent_id: str


@dataclass(frozen=True)
class VoiceSynthesisRequest:
    text: str
    profile_id: str


@dataclass(frozen=True)
class VoiceSynthesisResult:
    provider: str
    verification_state: str


@dataclass(frozen=True)
class VoiceDiagnostics:
    current_state: VoiceState
    audio_backend: str
    backend_available: bool
    queue_depth: int
    ring_buffer_duration_ms: int
    metrics: dict[str, int]
    discovery_status: str
    unavailable_reason: str | None
    device_count: int
    default_device_selector: str | None
    discovery_generation: int
    discovery_timestamp: datetime | None


@dataclass(frozen=True)
class VoiceDeviceDiscoveryResult:
    backend: str
    backend_available: bool
    discovery_status: str
    normalized_error: str | None
    devices: tuple[AudioDevice, ...]
    default_device_selector: str | None
    generation: int
    timestamp: datetime
    verification_state: str


@dataclass(frozen=True)
class VoiceDeviceSelectionResult:
    device: AudioDevice | None
    verification_state: str
    normalized_error: str | None = None


@dataclass(frozen=True)
class VoiceCaptureRequest:
    duration_seconds: int = 5
    device_selector: str | None = None


@dataclass(frozen=True)
class VoiceCaptureResult:
    operation: str
    device: AudioDevice | None
    requested_duration_seconds: int
    observed_duration_seconds: float
    frames_received: int
    frames_normalized: int
    frames_dropped: int
    malformed_frames: int
    queue_overruns: int
    callback_warnings: int
    sequence_gaps: int
    minimum_level: float | None
    maximum_level: float | None
    rms_level: float | None
    peak_level: float | None
    sample_rate: int
    channels: int
    verification_state: str
    normalized_error: str | None = None
    retryable: bool = False
    drop_ratio: float = 0.0
    normalized_audio_duration_seconds: float = 0.0
    cleanup_verified: bool = False
    worker_stopped: bool = False
    silent_frames: int = 0
    clipped_frames: int = 0
    total_samples: int = 0
    stream_opened: bool = False
    stream_stopped: bool = False
    stream_closed: bool = False
    worker_started: bool = False
    worker_failure: bool = False
    queue_drained: bool = False
    ring_buffer_cleared: bool = False
    resampler_reset: bool = False


class AudioInputAdapter(Protocol):
    def devices(self) -> list[AudioDevice]: ...
    def start(
        self,
        selector: str | None,
        on_frame: Callable[[tuple[float, ...], int, int, bool], None] | None = None,
    ) -> None: ...
    def stop(self) -> None: ...


class WakeWordEngine(Protocol):
    def detect(self, frames: tuple[AudioFrame, ...], session_id: str) -> WakeDetection | None: ...


class WakePhraseVerifier(Protocol):
    def verify(self, frames: tuple[AudioFrame, ...], phrase: str) -> VoiceOutcome: ...


class TranscriptionProvider(Protocol):
    def transcribe(self, frames: tuple[AudioFrame, ...]) -> TranscriptionResult: ...


class FakeAudioInputAdapter:
    def __init__(self, devices: list[AudioDevice] | None = None) -> None:
        self._devices = (
            devices
            if devices is not None
            else [AudioDevice("default", "Default microphone", True, 1)]
        )

    def devices(self) -> list[AudioDevice]:
        return list(self._devices)

    def start(
        self,
        selector: str | None,
        on_frame: Callable[[tuple[float, ...], int, int, bool], None] | None = None,
    ) -> None:
        pass

    def stop(self) -> None:
        pass


class FakeVad:
    def __init__(self, speech: bool = False) -> None:
        self.speech = speech

    def is_speech(self, frame: AudioFrame) -> bool:
        return self.speech

    def analyze(
        self, samples: tuple[float, ...], sample_rate: int, timestamp: float
    ) -> VoiceActivityResult:
        energy = sqrt(sum(value * value for value in samples) / len(samples)) if samples else 0.0
        return VoiceActivityResult(
            timestamp,
            float(self.speech),
            self.speech,
            energy,
            self.speech,
            len(samples) * 1000 / sample_rate,
        )

    def reset(self) -> None:
        pass


class FakeWakeWordEngine:
    def __init__(self, detection: WakeDetection | None = None) -> None:
        self.detection = detection

    def detect(self, frames: tuple[AudioFrame, ...], session_id: str) -> WakeDetection | None:
        return self.detection


class FakeWakePhraseVerifier:
    def __init__(self, outcome: VoiceOutcome = VoiceOutcome.CONFIRMED) -> None:
        self.outcome = outcome

    def verify(self, frames: tuple[AudioFrame, ...], phrase: str) -> VoiceOutcome:
        return self.outcome


class FakeTranscriptionProvider:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def transcribe(self, frames: tuple[AudioFrame, ...]) -> TranscriptionResult:
        return TranscriptionResult(self.text, self.text)


class WindowsAudioInputAdapter:
    def __init__(self) -> None:
        self._stream: Any | None = None
        self.unavailable_reason: str | None = None

    def devices(self) -> list[AudioDevice]:
        try:
            sd = import_module("sounddevice")
        except (ImportError, ModuleNotFoundError):
            self.unavailable_reason = "BACKEND_UNAVAILABLE"
            return []
        try:
            default = sd.default.device[0]
            raw_devices = sd.query_devices()
        except sd.PortAudioError:
            self.unavailable_reason = "PORTAUDIO_FAILURE"
            return []
        except OSError:
            self.unavailable_reason = "BACKEND_UNAVAILABLE"
            return []

        result: list[AudioDevice] = []
        malformed = False
        for i, item in enumerate(raw_devices):
            try:
                channels = int(item["max_input_channels"])
                name = str(item["name"])
                sample_rate = int(item["default_samplerate"])
            except (KeyError, TypeError, ValueError, OverflowError):
                malformed = True
                continue
            if channels > 0:
                result.append(
                    AudioDevice(f"mic-{i + 1}", name, i == default, channels, (16000,), sample_rate)
                )
        self.unavailable_reason = (
            "MALFORMED_DEVICE_METADATA"
            if malformed and not result
            else ("NO_INPUT_DEVICE" if not result else None)
        )
        return result

    def start(
        self,
        selector: str | None,
        on_frame: Callable[[tuple[float, ...], int, int, bool], None] | None = None,
    ) -> None:
        if on_frame is None:
            return
        sd = import_module("sounddevice")
        devices = self.devices()
        selected = next(
            (x for x in devices if x.selector == selector),
            next((x for x in devices if x.is_default), None),
        )
        if selected is None:
            raise RuntimeError("microphone unavailable")
        index = int(selected.selector.removeprefix("mic-")) - 1

        def callback(data: Any, frames: int, time_info: object, status: object) -> None:
            values = tuple(float(x) for row in data for x in row)
            on_frame(values, selected.channels, selected.default_sample_rate, bool(status))

        self._stream = sd.InputStream(
            device=index,
            channels=selected.channels,
            samplerate=selected.default_sample_rate,
            dtype="float32",
            blocksize=512,
            callback=callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class VadModelStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    INVALID_CHECKSUM = "INVALID_CHECKSUM"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    LOAD_FAILED = "LOAD_FAILED"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class SileroVadModelManifest:
    model_id: str
    backend: str
    version: str
    filename: str
    sha256: str
    download_source: str
    sample_rate: int = 16000
    window_size: int = 512
    license: str = "MIT"
    source_metadata: str = "k2-fsa/sherpa-onnx release asset"

    @classmethod
    def load(cls, path: Path) -> SileroVadModelManifest:
        data = json.loads(path.read_text(encoding="utf-8"))
        aliases = {
            "model_version": "version",
            "source_url": "download_source",
            "source_project": "source_metadata",
        }
        for source, target in aliases.items():
            if source in data:
                data[target] = data.pop(source)
        result = cls(**data)
        if (
            not path.name == "manifest.json"
            or len(result.sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in result.sha256)
            or Path(result.filename).name != result.filename
            or not result.download_source.startswith("https://")
            or result.sample_rate != 16000
            or result.window_size != 512
        ):
            raise ValueError("INVALID_VAD_MANIFEST")
        return result


class WaveDecodeError(StrEnum):
    WAV_FILE_NOT_FOUND = "WAV_FILE_NOT_FOUND"
    WAV_PATH_INVALID = "WAV_PATH_INVALID"
    WAV_FORMAT_INVALID = "WAV_FORMAT_INVALID"
    WAV_UNSUPPORTED = "WAV_UNSUPPORTED"
    WAV_TOO_LARGE = "WAV_TOO_LARGE"
    WAV_TOO_LONG = "WAV_TOO_LONG"
    WAV_EMPTY = "WAV_EMPTY"
    WAV_TRUNCATED = "WAV_TRUNCATED"
    WAV_DECODE_FAILED = "WAV_DECODE_FAILED"


@dataclass(frozen=True)
class WaveDecodeConfiguration:
    maximum_file_bytes: int = 20 * 1024 * 1024
    maximum_duration_seconds: int = 300
    maximum_decoded_samples: int = 4_800_000
    supported_channel_counts: tuple[int, ...] = (1, 2)
    supported_sample_widths: tuple[int, ...] = (1, 2, 3, 4)
    minimum_sample_rate: int = 8000
    maximum_sample_rate: int = 48000
    target_sample_rate: int = 16000
    target_channels: int = 1

    def __post_init__(self) -> None:
        numeric = (
            self.maximum_file_bytes,
            self.maximum_duration_seconds,
            self.maximum_decoded_samples,
            self.minimum_sample_rate,
            self.maximum_sample_rate,
            self.target_sample_rate,
            self.target_channels,
        )
        if any(not isfinite(float(value)) or value <= 0 for value in numeric) or (
            self.minimum_sample_rate > self.maximum_sample_rate
            or self.target_sample_rate != 16000
            or self.target_channels != 1
            or not self.supported_channel_counts
            or not self.supported_sample_widths
            or any(
                value <= 0 for value in self.supported_channel_counts + self.supported_sample_widths
            )
        ):
            raise ValueError("invalid WAV decode configuration")


@dataclass(frozen=True)
class WaveAudioMetadata:
    source_sample_rate: int
    source_channels: int
    source_sample_width: int
    source_frame_count: int
    source_duration_seconds: float
    normalized_sample_rate: int
    normalized_sample_count: int
    normalized_duration_seconds: float


@dataclass
class DecodedWaveAudio:
    filename: str
    metadata: WaveAudioMetadata
    _samples: list[float] = field(repr=False)
    samples_cleared: bool = False

    @property
    def samples(self) -> tuple[float, ...]:
        return tuple(self._samples)

    def public(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "source_sample_rate": self.metadata.source_sample_rate,
            "source_channels": self.metadata.source_channels,
            "source_sample_width": self.metadata.source_sample_width,
            "source_frame_count": self.metadata.source_frame_count,
            "source_duration_seconds": self.metadata.source_duration_seconds,
            "normalized_sample_rate": self.metadata.normalized_sample_rate,
            "normalized_sample_count": self.metadata.normalized_sample_count,
            "normalized_duration_seconds": self.metadata.normalized_duration_seconds,
            "total_samples": len(self._samples),
            "samples_cleared": self.samples_cleared,
        }

    def clear_samples(self) -> None:
        self._samples.clear()
        self.samples_cleared = True


@dataclass(frozen=True)
class WaveDecodeResult:
    audio: DecodedWaveAudio | None
    error: WaveDecodeError | None

    @property
    def verification_state(self) -> str:
        return "VERIFIED" if self.audio else "UNVERIFIED"


class BoundedWaveDecoder:
    """PCM-only local decoder; decoded samples never leave this in-memory boundary."""

    def __init__(self, config: WaveDecodeConfiguration | None = None) -> None:
        self.config = config or WaveDecodeConfiguration()

    def decode(self, path: Path) -> WaveDecodeResult:
        if not path.exists():
            return WaveDecodeResult(None, WaveDecodeError.WAV_FILE_NOT_FOUND)
        if not path.is_file():
            return WaveDecodeResult(None, WaveDecodeError.WAV_PATH_INVALID)
        if path.suffix.casefold() != ".wav":
            return WaveDecodeResult(None, WaveDecodeError.WAV_UNSUPPORTED)
        if path.stat().st_size > self.config.maximum_file_bytes:
            return WaveDecodeResult(None, WaveDecodeError.WAV_TOO_LARGE)
        try:
            with wave.open(str(path), "rb") as source:
                channels, width, rate, frames = (
                    source.getnchannels(),
                    source.getsampwidth(),
                    source.getframerate(),
                    source.getnframes(),
                )
                if (
                    channels not in self.config.supported_channel_counts
                    or width not in self.config.supported_sample_widths
                    or not self.config.minimum_sample_rate
                    <= rate
                    <= self.config.maximum_sample_rate
                ):
                    return WaveDecodeResult(None, WaveDecodeError.WAV_UNSUPPORTED)
                if not frames:
                    return WaveDecodeResult(None, WaveDecodeError.WAV_EMPTY)
                if frames > self.config.maximum_decoded_samples:
                    return WaveDecodeResult(None, WaveDecodeError.WAV_TOO_LONG)
                duration = frames / rate
                if duration > self.config.maximum_duration_seconds:
                    return WaveDecodeResult(None, WaveDecodeError.WAV_TOO_LONG)
                raw = source.readframes(frames)
                if len(raw) != frames * channels * width:
                    return WaveDecodeResult(None, WaveDecodeError.WAV_TRUNCATED)
        except (EOFError, OSError, wave.Error):
            return WaveDecodeResult(None, WaveDecodeError.WAV_FORMAT_INVALID)
        values: list[float] = []
        for offset in range(0, len(raw), width):
            part = raw[offset : offset + width]
            integer = int.from_bytes(part, "little", signed=width != 1)
            values.append(
                (integer - 128) / 128 if width == 1 else integer / float(1 << (width * 8 - 1))
            )
        mono = (
            values
            if channels == 1
            else [(values[i] + values[i + 1]) / 2 for i in range(0, len(values), 2)]
        )
        if not all(isfinite(value) and -1.0 <= value <= 1.0 for value in mono):
            return WaveDecodeResult(None, WaveDecodeError.WAV_DECODE_FAILED)
        target_count = round(len(mono) * self.config.target_sample_rate / rate)
        if target_count > self.config.maximum_decoded_samples:
            return WaveDecodeResult(None, WaveDecodeError.WAV_TOO_LONG)
        if rate != self.config.target_sample_rate:
            # Deterministic linear interpolation; output time positions are monotonic.
            normalized = [
                mono[min(int(index * rate / self.config.target_sample_rate), len(mono) - 1)]
                if len(mono) == 1
                else mono[int(index * rate / self.config.target_sample_rate)]
                + (
                    mono[min(int(index * rate / self.config.target_sample_rate) + 1, len(mono) - 1)]
                    - mono[int(index * rate / self.config.target_sample_rate)]
                )
                * ((index * rate / self.config.target_sample_rate) % 1)
                for index in range(target_count)
            ]
        else:
            normalized = mono
        if not normalized or not all(
            isfinite(value) and -1.0 <= value <= 1.0 for value in normalized
        ):
            return WaveDecodeResult(None, WaveDecodeError.WAV_DECODE_FAILED)
        metadata = WaveAudioMetadata(
            rate,
            channels,
            width,
            frames,
            duration,
            self.config.target_sample_rate,
            len(normalized),
            len(normalized) / self.config.target_sample_rate,
        )
        return WaveDecodeResult(DecodedWaveAudio(path.name, metadata, normalized), None)


class SherpaOnnxSileroVadAdapter:
    """Explicit single-owner Sherpa VAD boundary; it never opens an audio device."""

    def __init__(self, manifest: SileroVadModelManifest, model_path: Path) -> None:
        self.manifest, self.model_path = manifest, model_path
        self.status = VadModelStatus.MISSING
        self.initialized = False
        self.last_error: str | None = None
        self._detector: Any | None = None

    def _checksum_valid(self) -> bool:
        if not self.model_path.is_file():
            return False
        return (
            hashlib.sha256(self.model_path.read_bytes()).hexdigest().casefold()
            == self.manifest.sha256.casefold()
        )

    def initialize(self) -> VadModelStatus:
        if self.status == VadModelStatus.CLOSED:
            return self.status
        if self.initialized:
            return self.status
        if not self.model_path.is_file():
            self.status = VadModelStatus.MISSING
            return self.status
        if not self._checksum_valid():
            self.status = VadModelStatus.INVALID_CHECKSUM
            return self.status
        try:
            sherpa = import_module("sherpa_onnx")
        except (ImportError, ModuleNotFoundError):
            self.status = VadModelStatus.BACKEND_UNAVAILABLE
            return self.status
        try:
            config = sherpa.VadModelConfig(
                silero_vad=sherpa.SileroVadModelConfig(model=str(self.model_path))
            )
            self._detector = sherpa.VoiceActivityDetector(config, self.manifest.sample_rate)
            self.initialized = True
            self.status = VadModelStatus.AVAILABLE
        except (AttributeError, OSError, RuntimeError, ValueError):
            self.status = VadModelStatus.LOAD_FAILED
            self.last_error = "MODEL_INITIALIZATION_FAILED"
        return self.status

    def analyze(
        self, samples: tuple[float, ...], sample_rate: int, timestamp: float
    ) -> VoiceActivityResult:
        if not self.initialized or self._detector is None:
            raise RuntimeError("VAD_NOT_INITIALIZED")
        if (
            sample_rate != self.manifest.sample_rate
            or len(samples) != self.manifest.window_size
            or not all(isfinite(x) for x in samples)
        ):
            raise ValueError("INVALID_VAD_FRAME")
        try:
            # sherpa's detector owns streaming state; the public Python API accepts samples.
            self._detector.accept_waveform(sample_rate, list(samples))
            probability = float(getattr(self._detector, "probability", 0.0))
        except (AttributeError, OSError, RuntimeError, ValueError):
            self.last_error = "VAD_INFERENCE_FAILED"
            raise RuntimeError("VAD_INFERENCE_FAILED") from None
        if not isfinite(probability) or not 0 <= probability <= 1:
            raise RuntimeError("INVALID_VAD_PROBABILITY")
        energy = sqrt(sum(x * x for x in samples) / len(samples))
        return VoiceActivityResult(
            timestamp,
            probability,
            probability >= 0.5,
            energy,
            energy >= 0.01,
            len(samples) * 1000 / sample_rate,
            "sherpa-onnx-silero",
        )

    def reset(self) -> None:
        if self.initialized and self._detector is not None:
            try:
                self._detector.reset()
            except (AttributeError, OSError, RuntimeError):
                self.last_error = "VAD_RESET_FAILED"

    def close(self) -> None:
        if self.status == VadModelStatus.CLOSED:
            return
        self._detector = None
        self.initialized = False
        self.status = VadModelStatus.CLOSED

    def diagnostics(self) -> dict[str, object]:
        return {
            "vad_backend": self.manifest.backend,
            "vad_model_id": self.manifest.model_id,
            "vad_model_version": self.manifest.version,
            "vad_model_status": self.status,
            "vad_model_path_sanitized": self.model_path.name,
            "vad_backend_available": self.status != VadModelStatus.BACKEND_UNAVAILABLE,
            "vad_initialized": self.initialized,
            "vad_last_error": self.last_error,
            "vad_configuration": {
                "sample_rate": self.manifest.sample_rate,
                "window_size": self.manifest.window_size,
            },
        }


@dataclass(frozen=True)
class VadFileInferenceResult:
    operation: str = "vad-file-test"
    sanitized_input_name: str = ""
    source_sample_rate: int = 0
    source_channels: int = 0
    source_sample_width: int = 0
    source_frame_count: int = 0
    source_duration_seconds: float = 0.0
    normalized_sample_rate: int = 16000
    normalized_sample_count: int = 0
    normalized_duration_seconds: float = 0.0
    processed_window_count: int = 0
    padded_sample_count: int = 0
    average_vad_probability: float = 0.0
    maximum_vad_probability: float = 0.0
    vad_positive_frame_count: int = 0
    energy_gate_positive_frame_count: int = 0
    speech_candidate_count: int = 0
    accepted_segment_count: int = 0
    rejected_candidate_count: int = 0
    segments: tuple[dict[str, object], ...] = ()
    detector_name: str = "unknown"
    detector_reset_before: bool = False
    detector_reset_after: bool = False
    decoded_samples_cleared: bool = False
    segment_samples_cleared: bool = False
    cleanup_verified: bool = False
    verification_state: str = "UNVERIFIED"
    normalized_error: str | None = None
    retryable: bool = False

    def public(self) -> dict[str, object]:
        return self.__dict__.copy()


class VadInferenceError(RuntimeError):
    """Expected, normalized detector inference failure."""


class VadDetectorUnavailableError(VadInferenceError):
    """The injected detector cannot process a file frame."""


class VadInvalidProbabilityError(VadInferenceError):
    """The detector returned a malformed activity result."""


class VadDetectorResetError(VadInferenceError):
    """The detector could not reset its bounded file-inference state."""


class VadFileInferenceService:
    """File-only orchestration.  It owns neither a microphone nor a model loader."""

    def __init__(
        self,
        decoder: BoundedWaveDecoder,
        detector: VoiceActivityDetector,
        controller_factory: Callable[[], SpeechSegmentController],
        config: VadConfiguration,
    ) -> None:
        self.decoder, self.detector, self.controller_factory, self.config = (
            decoder,
            detector,
            controller_factory,
            config,
        )

    def infer(self, path: Path) -> VadFileInferenceResult:
        decoded = self.decoder.decode(path)
        if decoded.audio is None:
            return VadFileInferenceResult(
                sanitized_input_name=path.name,
                normalized_error=decoded.error.value
                if decoded.error
                else WaveDecodeError.WAV_DECODE_FAILED.value,
            )
        audio = decoded.audio
        meta = audio.metadata
        common = {
            "sanitized_input_name": audio.filename,
            "source_sample_rate": meta.source_sample_rate,
            "source_channels": meta.source_channels,
            "source_sample_width": meta.source_sample_width,
            "source_frame_count": meta.source_frame_count,
            "source_duration_seconds": meta.source_duration_seconds,
            "normalized_sample_rate": meta.normalized_sample_rate,
            "normalized_sample_count": meta.normalized_sample_count,
            "normalized_duration_seconds": meta.normalized_duration_seconds,
            "detector_name": type(self.detector).__name__,
        }
        before = after = False
        segments: list[SpeechSegment] = []
        probabilities: list[float] = []
        vad_positive = energy_positive = 0
        controller = self.controller_factory()
        error: str | None = None
        padded = 0
        try:
            self.detector.reset()
            before = True
            values = audio.samples
            for offset in range(0, len(values), self.config.window_size):
                window = values[offset : offset + self.config.window_size]
                if len(window) < self.config.window_size:
                    padded += self.config.window_size - len(window)
                    window += (0.0,) * (self.config.window_size - len(window))
                activity = self.detector.analyze(
                    window, self.config.sample_rate, offset / self.config.sample_rate
                )
                if not activity.valid():
                    raise VadInvalidProbabilityError
                probabilities.append(activity.probability)
                vad_positive += int(activity.is_speech)
                energy_positive += int(activity.energy_gate_passed)
                result = controller.process(
                    AudioFrame(
                        window, offset / self.config.sample_rate, offset // self.config.window_size
                    ),
                    activity,
                    "vad-file",
                )
                if result:
                    segments.append(result)
            if error is None:
                terminal = controller.end_of_file()
                if terminal:
                    segments.append(terminal)
        except VadInvalidProbabilityError:
            error = "VAD_INVALID_PROBABILITY"
        except (VadInferenceError, RuntimeError, ValueError):
            error = "VAD_INFERENCE_FAILED"
        finally:
            try:
                self.detector.reset()
                after = True
            except (VadDetectorResetError, RuntimeError, ValueError):
                error = error or "VAD_CLEANUP_UNVERIFIED"
            audio.clear_samples()
        safe = tuple(item.public() for item in segments)
        for item in segments:
            item.clear_samples()
        cleaned = audio.samples_cleared and all(not item.samples for item in segments) and after
        if not cleaned:
            error = error or "VAD_CLEANUP_UNVERIFIED"
        return VadFileInferenceResult(
            **cast(Any, common),
            processed_window_count=len(probabilities),
            padded_sample_count=padded,
            average_vad_probability=sum(probabilities) / len(probabilities)
            if probabilities
            else 0.0,
            maximum_vad_probability=max(probabilities, default=0.0),
            vad_positive_frame_count=vad_positive,
            energy_gate_positive_frame_count=energy_positive,
            accepted_segment_count=len(safe),
            segments=safe,
            detector_reset_before=before,
            detector_reset_after=after,
            decoded_samples_cleared=audio.samples_cleared,
            segment_samples_cleared=all(not item.samples for item in segments),
            cleanup_verified=cleaned,
            verification_state="VERIFIED" if error is None and cleaned else "UNVERIFIED",
            normalized_error=error,
            retryable=error in {"VAD_UNAVAILABLE", "VAD_INFERENCE_FAILED"},
        )


VadFileTestResult = VadFileInferenceResult


def run_vad_file_test(path: Path, adapter: SherpaOnnxSileroVadAdapter) -> VadFileInferenceResult:
    """Compatibility entry point; new callers should inject dependencies directly."""
    if adapter.initialize() != VadModelStatus.AVAILABLE:
        return VadFileInferenceResult(
            sanitized_input_name=path.name, normalized_error="VAD_UNAVAILABLE"
        )
    config = VadConfiguration()
    return VadFileInferenceService(
        BoundedWaveDecoder(), adapter, lambda: SpeechSegmentController(config), config
    ).infer(path)


class SherpaOnnxSileroVad(FakeVad):
    """Legacy fake boundary retained for Phase 1A capture composition."""


class SherpaOnnxWakeWordEngine(FakeWakeWordEngine):
    """Default KWS boundary; model installation is explicit, never startup work."""


class LegacyPanguOnnxWakeWordEngine(FakeWakeWordEngine):
    """Disabled compatibility-only legacy engine; never selected by RuntimeBuilder."""


class FasterWhisperTranscriptionProvider(FakeTranscriptionProvider):
    """Local transcription boundary; model loading remains explicit and bounded."""


class VoiceSessionRuntime:
    _allowed: ClassVar[dict[VoiceState, frozenset[VoiceState]]] = {
        VoiceState.STOPPED: frozenset({VoiceState.INITIALIZING}),
        VoiceState.INITIALIZING: frozenset(
            {VoiceState.IDLE_LISTENING, VoiceState.DEVICE_UNAVAILABLE}
        ),
        VoiceState.IDLE_LISTENING: frozenset(
            {VoiceState.SPEECH_CANDIDATE, VoiceState.SHUTTING_DOWN}
        ),
        VoiceState.SPEECH_CANDIDATE: frozenset(
            {VoiceState.WAKE_CANDIDATE, VoiceState.IDLE_LISTENING}
        ),
        VoiceState.WAKE_CANDIDATE: frozenset({VoiceState.WAKE_CONFIRMED, VoiceState.COOLDOWN}),
        VoiceState.WAKE_CONFIRMED: frozenset({VoiceState.COMMAND_LISTENING}),
        VoiceState.COMMAND_LISTENING: frozenset({VoiceState.TURN_ENDING, VoiceState.SHUTTING_DOWN}),
        VoiceState.TURN_ENDING: frozenset({VoiceState.TRANSCRIBING}),
        VoiceState.TRANSCRIBING: frozenset({VoiceState.COMMAND_READY}),
        VoiceState.COMMAND_READY: frozenset({VoiceState.COOLDOWN}),
        VoiceState.COOLDOWN: frozenset({VoiceState.IDLE_LISTENING}),
        VoiceState.DEVICE_UNAVAILABLE: frozenset({VoiceState.SHUTTING_DOWN}),
        VoiceState.SHUTTING_DOWN: frozenset({VoiceState.STOPPED}),
    }

    def __init__(
        self,
        input: AudioInputAdapter,
        vad: VoiceActivityDetector,
        wake: WakeWordEngine,
        verifier: WakePhraseVerifier,
        transcriber: TranscriptionProvider,
        events: EventBus,
        language: LanguageRuntime,
        config: VoiceConfig | None = None,
    ) -> None:
        (
            self.input,
            self.vad,
            self.wake,
            self.verifier,
            self.transcriber,
            self.events,
            self.language,
            self.config,
        ) = input, vad, wake, verifier, transcriber, events, language, config or VoiceConfig()
        self.state = VoiceState.STOPPED
        self.frames = AudioRingBuffer()
        self.queue: Queue[object] = Queue(maxsize=64)
        self.resampler = LocalAudioResampler()
        self.metrics = {
            "captured_frames": 0,
            "dropped_frames": 0,
            "wake_candidates": 0,
            "wake_confirmations": 0,
            "wake_rejections": 0,
            "frames_received": 0,
            "frames_normalized": 0,
            "frames_dropped": 0,
            "malformed_frames": 0,
            "callback_warnings": 0,
            "queue_overruns": 0,
            "sequence_gaps": 0,
            "stream_start_count": 0,
            "stream_stop_count": 0,
        }
        self.session_id = ""
        self._sequence = 0
        self._accepting_frames = False
        self.metrics["device_disconnects"] = 0
        self._worker: Thread | None = None
        self._worker_stop = Event()
        self._normalized_samples = 0
        self._sum_squares = 0.0
        self._minimum_level: float | None = None
        self._maximum_level: float | None = None
        self._clipped_samples = 0
        self._discovery: VoiceDeviceDiscoveryResult | None = None
        self._discovery_generation = 0

    def _start_worker(self) -> None:
        self._worker_stop.clear()

        def run() -> None:
            while not self._worker_stop.is_set():
                item = self.queue.get()
                try:
                    if item is None:
                        return
                    samples, channels, rate, timestamp, sequence = cast(
                        tuple[tuple[float, ...], int, int, float, int], item
                    )
                    frame = self.resampler.normalize(samples, channels, rate, timestamp, sequence)
                    if frame is None:
                        self.metrics["malformed_frames"] += 1
                    else:
                        self.metrics["frames_normalized"] += 1
                        self.frames.append(frame)
                        self._normalized_samples += len(frame.samples)
                        for sample in frame.samples:
                            clipped = max(-1.0, min(1.0, sample))
                            self._clipped_samples += int(clipped != sample)
                            level = abs(clipped)
                            self._minimum_level = (
                                level
                                if self._minimum_level is None
                                else min(self._minimum_level, level)
                            )
                            self._maximum_level = (
                                level
                                if self._maximum_level is None
                                else max(self._maximum_level, level)
                            )
                            self._sum_squares += clipped * clipped
                finally:
                    self.queue.task_done()

        self._worker = Thread(target=run, name="pangu-voice-audio", daemon=True)
        self._worker.start()

    def accept_callback_frame(
        self, samples: tuple[float, ...], channels: int, rate: int, warning: bool = False
    ) -> None:
        if not self._accepting_frames:
            return
        self.metrics["frames_received"] += 1
        self._sequence += 1
        if warning:
            self.metrics["callback_warnings"] += 1
        try:
            self.queue.put_nowait(
                (samples, channels, rate, __import__("time").monotonic(), self._sequence)
            )
        except Full:
            self.metrics["frames_dropped"] += 1
            self.metrics["queue_overruns"] += 1

    async def _transition(self, state: VoiceState, event: str) -> None:
        if state not in self._allowed.get(self.state, set()):
            raise RuntimeError("illegal voice state transition")
        self.state = state
        await self.events.publish(
            EventEnvelope(event, {"session_id": self.session_id, "state": state})
        )

    async def start(self, selector: str | None = None, capture: bool = False) -> None:
        await self._transition(VoiceState.INITIALIZING, "voice.runtime.started")
        devices = self.discover_devices().devices
        if not devices:
            await self._transition(VoiceState.DEVICE_UNAVAILABLE, "voice.device.disconnected")
            return
        self.session_id = str(uuid4())
        if capture:
            self._accepting_frames = True
            self.input.start(selector, self.accept_callback_frame)
            self.metrics["stream_start_count"] += 1
        await self._transition(VoiceState.IDLE_LISTENING, "voice.device.selected")

    async def stop(self) -> None:
        if self.state == VoiceState.STOPPED:
            return
        await self._transition(VoiceState.SHUTTING_DOWN, "voice.runtime.stopped")
        self.input.stop()
        self._accepting_frames = False
        self.frames.clear()
        self.metrics["stream_stop_count"] += 1
        self.vad.reset()
        await self._transition(VoiceState.STOPPED, "voice.runtime.stopped")

    def discover_devices(self, refresh: bool = False) -> VoiceDeviceDiscoveryResult:
        if self._discovery is not None and not refresh:
            return self._discovery
        devices = tuple(self.input.devices())
        reason = getattr(self.input, "unavailable_reason", None)
        status = "AVAILABLE" if devices else (reason or "NO_INPUT_DEVICE")
        self._discovery_generation += 1
        result = VoiceDeviceDiscoveryResult(
            "sounddevice",
            status not in {"BACKEND_UNAVAILABLE", "PORTAUDIO_FAILURE"},
            status,
            None if status == "AVAILABLE" else status,
            devices,
            next((x.selector for x in devices if x.is_default), None),
            self._discovery_generation,
            datetime.now(UTC),
            "VERIFIED" if status == "AVAILABLE" else "UNVERIFIED",
        )
        self._discovery = result
        return result

    def devices(self) -> list[AudioDevice]:
        return list(self.discover_devices().devices)

    def select_device(self, selector: str | None = None) -> VoiceDeviceSelectionResult:
        discovery = self.discover_devices(refresh=True)
        devices = list(discovery.devices)
        if not devices:
            return VoiceDeviceSelectionResult(None, "UNVERIFIED", discovery.normalized_error)
        if selector is not None:
            selected = next((item for item in devices if item.selector == selector), None)
            return VoiceDeviceSelectionResult(
                selected,
                "VERIFIED" if selected else "UNVERIFIED",
                None if selected else "STALE_DEVICE_SELECTOR",
            )
        defaults = [item for item in devices if item.is_default]
        if len(defaults) == 1:
            return VoiceDeviceSelectionResult(defaults[0], "VERIFIED")
        if len(devices) == 1:
            return VoiceDeviceSelectionResult(devices[0], "VERIFIED")
        return VoiceDeviceSelectionResult(None, "UNVERIFIED", "AMBIGUOUS_DEVICE")

    async def capture_test(self, request: VoiceCaptureRequest) -> VoiceCaptureResult:
        if not 1 <= request.duration_seconds <= 30:
            return VoiceCaptureResult(
                "capture-test",
                None,
                request.duration_seconds,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                None,
                None,
                16000,
                0,
                "UNVERIFIED",
                "INVALID_DURATION",
            )
        selection = self.select_device(request.device_selector)
        if selection.device is None:
            return VoiceCaptureResult(
                "capture-test",
                None,
                request.duration_seconds,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                None,
                None,
                16000,
                0,
                "UNVERIFIED",
                selection.normalized_error,
            )
        before = dict(self.metrics)
        start = asyncio.get_running_loop().time()
        failure: str | None = None
        observed = 0.0
        try:
            self._accepting_frames = True
            self._normalized_samples = 0
            self._sum_squares = 0.0
            self._minimum_level = self._maximum_level = None
            self._clipped_samples = 0
            self._start_worker()
            self.input.start(selection.device.selector, self.accept_callback_frame)
            await self.events.publish(
                EventEnvelope(
                    "voice.capture.started",
                    {"session_id": self.session_id, "device_selector": selection.device.selector},
                )
            )
            await asyncio.sleep(request.duration_seconds)
            observed = asyncio.get_running_loop().time() - start
        except asyncio.CancelledError:
            failure = "CANCELLED"
        except DeviceDisconnectedError:
            self.metrics["device_disconnects"] += 1
            failure = "DEVICE_DISCONNECTED"
        except (OSError, RuntimeError):
            failure = "STREAM_FAILURE"
        finally:
            self._accepting_frames = False
            try:
                self.input.stop()
            except (OSError, RuntimeError):
                failure = "STREAM_CLOSE_FAILURE"
            self.queue.put(None)
            if self._worker:
                self._worker.join(timeout=2)
                worker_stopped = not self._worker.is_alive()
                self._worker = None
            else:
                worker_stopped = False
            self.frames.clear()
        received = self.metrics["frames_received"] - before["frames_received"]
        normalized = self.metrics["frames_normalized"] - before["frames_normalized"]
        dropped = self.metrics["frames_dropped"] - before["frames_dropped"]
        ratio = dropped / max(received + dropped, 1)
        coverage = self._normalized_samples / 16000
        cleanup = worker_stopped and not self.frames.duration_ms()
        error = failure or (
            "WORKER_NOT_STOPPED"
            if not worker_stopped
            else "CLEANUP_UNVERIFIED"
            if not cleanup
            else "EXCESSIVE_FRAME_LOSS"
            if ratio > self.config.maximum_verified_drop_ratio
            else "NO_USABLE_FRAMES"
            if not normalized
            else None
        )
        state = "VERIFIED" if error is None else "UNVERIFIED"
        result = replace(
            VoiceCaptureResult(
                "capture-test",
                selection.device,
                request.duration_seconds,
                observed,
                received,
                normalized,
                dropped,
                self.metrics["malformed_frames"] - before["malformed_frames"],
                self.metrics["queue_overruns"] - before["queue_overruns"],
                self.metrics["callback_warnings"] - before["callback_warnings"],
                0,
                self._minimum_level,
                self._maximum_level,
                sqrt(self._sum_squares / self._normalized_samples)
                if self._normalized_samples
                else None,
                self._maximum_level,
                16000,
                selection.device.channels,
                state,
                error,
                state != "VERIFIED",
                ratio,
                coverage,
                cleanup,
                worker_stopped,
            ),
            total_samples=self._normalized_samples,
            silent_frames=0,
            clipped_frames=self._clipped_samples,
            stream_opened=failure is None,
            stream_stopped=failure != "STREAM_CLOSE_FAILURE",
            stream_closed=failure != "STREAM_CLOSE_FAILURE",
            worker_started=True,
            queue_drained=self.queue.unfinished_tasks == 0,
            ring_buffer_cleared=self.frames.duration_ms() == 0,
            resampler_reset=True,
        )
        event = (
            "voice.capture.completed"
            if result.verification_state == "VERIFIED"
            else "voice.capture.cancelled"
            if result.normalized_error == "CANCELLED"
            else "voice.device.disconnected"
            if result.normalized_error == "DEVICE_DISCONNECTED"
            else "voice.capture.failed"
        )
        await self.events.publish(
            EventEnvelope(
                event,
                {
                    "session_id": self.session_id,
                    "device_selector": selection.device.selector,
                    "frames_received": result.frames_received,
                    "drop_ratio": result.drop_ratio,
                    "verification_state": result.verification_state,
                    "normalized_error": result.normalized_error,
                },
            )
        )
        return result

    def diagnostics(self) -> VoiceDiagnostics:
        discovery = self.discover_devices()
        return VoiceDiagnostics(
            self.state,
            "sounddevice",
            discovery.backend_available,
            self.queue.qsize(),
            self.frames.duration_ms(),
            dict(self.metrics),
            discovery.discovery_status,
            discovery.normalized_error,
            len(discovery.devices),
            discovery.default_device_selector,
            discovery.generation,
            discovery.timestamp,
        )
