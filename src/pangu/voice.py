"""Local, bounded input-side voice runtime; no recording or speech output."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from importlib import import_module
from math import isfinite, sqrt
from queue import Full, Queue
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


class VoiceActivityDetector(Protocol):
    def is_speech(self, frame: AudioFrame) -> bool: ...
    def reset(self) -> None: ...


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


class SherpaOnnxSileroVad(FakeVad):
    """Placeholder until an installed declared Silero model is selected."""


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
