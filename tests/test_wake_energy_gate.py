from pathlib import Path

from pangu.voice import AudioFrame
from pangu.wake_word import SherpaKeywordSpotterWakeWordEngine, WakeWordConfig


def _config(tmp_path: Path) -> WakeWordConfig:
    return WakeWordConfig(
        model_root=tmp_path,
        keywords_file=tmp_path / "keywords.txt",
        encoder=tmp_path / "encoder.onnx",
        decoder=tmp_path / "decoder.onnx",
        joiner=tmp_path / "joiner.onnx",
        tokens=tmp_path / "tokens.txt",
        minimum_energy=0.008,
        maximum_window_seconds=3.0,
    )


def test_short_natural_phrase_is_not_diluted_by_surrounding_silence(tmp_path: Path) -> None:
    engine = SherpaKeywordSpotterWakeWordEngine(_config(tmp_path))
    silence = AudioFrame(tuple([0.0] * 512), 0.0, 1)
    speech = AudioFrame(tuple([0.02] * 512), 0.0, 2)

    frames = tuple([silence] * 60 + [speech] * 8 + [silence] * 20)

    assert engine._window_is_eligible(frames) is True


def test_single_energy_spike_does_not_open_wake_verifier(tmp_path: Path) -> None:
    engine = SherpaKeywordSpotterWakeWordEngine(_config(tmp_path))
    silence = AudioFrame(tuple([0.0] * 512), 0.0, 1)
    spike = AudioFrame(tuple([0.08] * 512), 0.0, 2)

    frames = tuple([silence] * 40 + [spike] + [silence] * 40)

    assert engine._window_is_eligible(frames) is False
