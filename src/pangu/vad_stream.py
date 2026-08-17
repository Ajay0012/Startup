from __future__ import annotations

from math import isfinite, sqrt
from typing import Any

from .voice import (
    VadDecisionSource,
    VoiceActivityDetector,
    VoiceActivityResult,
)


class StreamingVadFrameAdapter:
    """Adapt variable realtime audio callback frames to fixed-size VAD windows.

    Windows/PortAudio callbacks are resampled to 16 kHz after capture, so a native
    512-sample callback does not necessarily remain 512 samples after resampling.
    Silero VAD requires exact 512-sample windows. This adapter owns the small rolling
    sample buffer needed to bridge that impedance mismatch without opening a second
    microphone or changing the public realtime frame cadence.
    """

    def __init__(
        self,
        detector: VoiceActivityDetector,
        *,
        sample_rate: int = 16000,
        window_size: int = 512,
        minimum_energy_floor: float = 0.01,
    ) -> None:
        if sample_rate != 16000 or window_size <= 0:
            raise ValueError("invalid streaming VAD adapter configuration")
        self.detector = detector
        self.sample_rate = sample_rate
        self.window_size = window_size
        self.minimum_energy_floor = minimum_energy_floor
        self._pending: list[float] = []
        self._last_detected = False

    def analyze(
        self, samples: tuple[float, ...], sample_rate: int, timestamp: float
    ) -> VoiceActivityResult:
        if (
            sample_rate != self.sample_rate
            or not samples
            or not all(isfinite(value) and -1.0 <= value <= 1.0 for value in samples)
        ):
            raise ValueError("INVALID_STREAMING_VAD_FRAME")

        self._pending.extend(samples)
        while len(self._pending) >= self.window_size:
            window = tuple(self._pending[: self.window_size])
            del self._pending[: self.window_size]
            native = self.detector.analyze(window, self.sample_rate, timestamp)
            self._last_detected = native.is_speech

        energy = sqrt(sum(value * value for value in samples) / len(samples))
        return VoiceActivityResult(
            timestamp=timestamp,
            probability=None,
            is_speech=self._last_detected,
            energy_level=energy,
            energy_gate_passed=energy >= self.minimum_energy_floor,
            frame_duration_ms=len(samples) * 1000 / self.sample_rate,
            detector_name="streaming-fixed-window-vad",
            confidence_available=False,
            decision_source=VadDecisionSource.AUTHORITATIVE_BACKEND,
            threshold_applied_by_backend=True,
        )

    def reset(self) -> None:
        self._pending.clear()
        self._last_detected = False
        self.detector.reset()

    def flush(self) -> None:
        if self._pending:
            window = tuple(self._pending) + (0.0,) * (self.window_size - len(self._pending))
            self._pending.clear()
            native = self.detector.analyze(window, self.sample_rate, 0.0)
            self._last_detected = native.is_speech
        flush = getattr(self.detector, "flush", None)
        if callable(flush):
            flush()

    def close(self) -> None:
        self._pending.clear()
        self._last_detected = False
        close = getattr(self.detector, "close", None)
        if callable(close):
            close()

    def diagnostics(self) -> dict[str, object]:
        diagnostics = getattr(self.detector, "diagnostics", None)
        base: dict[str, object] = dict(diagnostics()) if callable(diagnostics) else {}
        base.update(
            {
                "streaming_vad_adapter": "fixed-window-buffer",
                "streaming_vad_window_size": self.window_size,
                "streaming_vad_pending_samples": len(self._pending),
            }
        )
        return base

    def initialize(self) -> Any:
        initialize = getattr(self.detector, "initialize", None)
        if callable(initialize):
            return initialize()
        return None
