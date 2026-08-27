from pathlib import Path

import pytest

from pangu.voice import AudioFrame
from pangu.wake_word import (
    SherpaKeywordSpotterWakeWordEngine,
    WakeWordConfig,
    load_wake_word_config,
)


class FakeStream:
    def __init__(self) -> None:
        self.samples: list[float] = []

    def accept_waveform(self, sample_rate: int, samples: list[float]) -> None:
        assert sample_rate == 16000
        self.samples.extend(samples)

    def input_finished(self) -> None:
        return None


class FakeSpotter:
    def __init__(self, result: str) -> None:
        self.result = result
        self.decoded = False

    def create_stream(self) -> FakeStream:
        return FakeStream()

    def is_ready(self, stream: FakeStream) -> bool:
        return not self.decoded

    def decode_stream(self, stream: FakeStream) -> None:
        self.decoded = True

    def get_result(self, stream: FakeStream) -> str:
        return self.result

    def reset_stream(self, stream: FakeStream) -> None:
        return None


def config(tmp_path: Path) -> WakeWordConfig:
    root = tmp_path / "wake"
    root.mkdir()
    files = {
        "encoder": root / "encoder.onnx",
        "decoder": root / "decoder.onnx",
        "joiner": root / "joiner.onnx",
        "tokens": root / "tokens.txt",
        "keywords": root / "keywords.txt",
    }
    for path in files.values():
        path.write_bytes(b"fixture")
    return WakeWordConfig(
        model_root=root,
        encoder=files["encoder"],
        decoder=files["decoder"],
        joiner=files["joiner"],
        tokens=files["tokens"],
        keywords_file=files["keywords"],
        cooldown_seconds=2.0,
    )


def frames(level: float = 0.2) -> tuple[AudioFrame, ...]:
    return (
        AudioFrame((level,) * 512, 1.0, 1),
        AudioFrame((level,) * 512, 1.032, 2),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HEY_PANGU", "HEY_PANGU"),
        ("hey pangu", "HEY_PANGU"),
        ("Hay Pangu", "HAY_PANGU"),
        ("Pangu", "PANGU"),
        ("Hey Panguu", "HEY_PANGUU"),
    ],
)
def test_wake_label_variants_are_normalized(raw: str, expected: str) -> None:
    assert SherpaKeywordSpotterWakeWordEngine.normalize_label(raw) == expected


def test_missing_model_is_truthfully_unavailable(tmp_path: Path) -> None:
    engine = SherpaKeywordSpotterWakeWordEngine(load_wake_word_config(tmp_path))
    health = engine.health()
    assert health.status == "UNAVAILABLE"
    assert health.normalized_error == "WAKE_MODEL_UNAVAILABLE"
    assert "encoder.onnx" in health.missing_files


def test_silence_is_rejected_before_backend_load(tmp_path: Path) -> None:
    engine = SherpaKeywordSpotterWakeWordEngine(config(tmp_path))
    assert engine.detect(frames(0.0), "session") is None
    assert engine.health().status == "READY_TO_LOAD"


def test_detects_hey_pangu_and_canonicalizes_phrase(tmp_path: Path) -> None:
    engine = SherpaKeywordSpotterWakeWordEngine(config(tmp_path))
    engine._spotter = FakeSpotter("HEY_PANGU")  # deterministic adapter fixture
    detection = engine.detect(frames(), "session-1")
    assert detection is not None
    assert detection.normalized_keyword == "HEY PANGU"
    assert detection.audio_session_id == "session-1"
    assert detection.engine_name == "sherpa-onnx-open-vocabulary-kws"


def test_unknown_keyword_is_rejected(tmp_path: Path) -> None:
    engine = SherpaKeywordSpotterWakeWordEngine(config(tmp_path))
    engine._spotter = FakeSpotter("HEY_GOOGLE")
    assert engine.detect(frames(), "session") is None


def test_cooldown_blocks_immediate_retrigger(tmp_path: Path) -> None:
    engine = SherpaKeywordSpotterWakeWordEngine(config(tmp_path))
    engine._spotter = FakeSpotter("HEY_PANGU")
    assert engine.detect(frames(), "session") is not None
    engine._spotter = FakeSpotter("HEY_PANGU")
    assert engine.detect(frames(), "session") is None


def test_tts_suppression_blocks_wake(tmp_path: Path) -> None:
    engine = SherpaKeywordSpotterWakeWordEngine(config(tmp_path))
    engine._spotter = FakeSpotter("HEY_PANGU")
    engine.suppress_for(1.0)
    assert engine.detect(frames(), "session") is None
