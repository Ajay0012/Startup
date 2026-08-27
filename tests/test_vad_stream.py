from __future__ import annotations

from pangu.vad_stream import StreamingVadFrameAdapter
from pangu.voice import VadDecisionSource, VoiceActivityResult


class _ExactWindowDetector:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.reset_calls = 0

    def analyze(
        self, samples: tuple[float, ...], sample_rate: int, timestamp: float
    ) -> VoiceActivityResult:
        if len(samples) != 512:
            raise ValueError("INVALID_VAD_FRAME")
        self.calls.append(len(samples))
        return VoiceActivityResult(
            timestamp=timestamp,
            probability=None,
            is_speech=True,
            energy_level=0.2,
            energy_gate_passed=True,
            frame_duration_ms=32.0,
            detector_name="exact-window",
            confidence_available=False,
            decision_source=VadDecisionSource.AUTHORITATIVE_BACKEND,
            threshold_applied_by_backend=True,
        )

    def reset(self) -> None:
        self.reset_calls += 1


def test_streaming_vad_buffers_resampled_callback_sizes_into_512_windows() -> None:
    native = _ExactWindowDetector()
    adapter = StreamingVadFrameAdapter(native)

    first = adapter.analyze((0.05,) * 186, 16000, 1.0)
    second = adapter.analyze((0.05,) * 186, 16000, 1.01)
    third = adapter.analyze((0.05,) * 186, 16000, 1.02)

    assert first.is_speech is False
    assert second.is_speech is False
    assert third.is_speech is True
    assert native.calls == [512]
    assert third.frame_duration_ms == 186 * 1000 / 16000


def test_streaming_vad_carries_native_state_between_fixed_windows() -> None:
    native = _ExactWindowDetector()
    adapter = StreamingVadFrameAdapter(native)

    adapter.analyze((0.05,) * 512, 16000, 1.0)
    carried = adapter.analyze((0.05,) * 186, 16000, 1.01)

    assert carried.is_speech is True
    assert native.calls == [512]


def test_streaming_vad_reset_clears_pending_and_native_state() -> None:
    native = _ExactWindowDetector()
    adapter = StreamingVadFrameAdapter(native)

    adapter.analyze((0.05,) * 300, 16000, 1.0)
    assert adapter.diagnostics()["streaming_vad_pending_samples"] == 300

    adapter.reset()

    assert native.reset_calls == 1
    assert adapter.diagnostics()["streaming_vad_pending_samples"] == 0
