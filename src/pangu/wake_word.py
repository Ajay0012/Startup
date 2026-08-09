from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from time import monotonic
from typing import Any

from .voice import AudioFrame, WakeDetection


@dataclass(frozen=True)
class WakeWordConfig:
    model_root: Path
    keywords_file: Path
    encoder: Path
    decoder: Path
    joiner: Path
    tokens: Path
    provider: str = "cpu"
    num_threads: int = 2
    cooldown_seconds: float = 2.0
    minimum_energy: float = 0.008
    maximum_window_seconds: float = 3.0
    accepted_labels: tuple[str, ...] = (
        "PANGU",
        "HEY_PANGU",
        "HAY_PANGU",
        "HEY_PANGUU",
        "HEY_PANGOO",
    )


@dataclass(frozen=True)
class WakeWordHealth:
    status: str
    backend: str
    model_root: str
    missing_files: tuple[str, ...] = ()
    normalized_error: str | None = None


class SherpaKeywordSpotterWakeWordEngine:
    """Local, fail-closed sherpa-onnx keyword spotter for PANGU.

    The detector is deliberately binary at this boundary: sherpa-onnx owns the
    keyword trigger threshold inside the generated keywords file. PANGU then
    applies its own energy floor, accepted-label allowlist, cooldown and TTS
    suppression before emitting a wake detection.
    """

    def __init__(self, config: WakeWordConfig) -> None:
        self.config = config
        self._spotter: Any | None = None
        self._last_detection = float("-inf")
        self._suppressed_until = float("-inf")
        self._last_error: str | None = None

    @staticmethod
    def normalize_label(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
        aliases = {
            "pangu": "PANGU",
            "hey_pangu": "HEY_PANGU",
            "hay_pangu": "HAY_PANGU",
            "hey_panguu": "HEY_PANGUU",
            "hey_pangoo": "HEY_PANGOO",
        }
        return aliases.get(cleaned, cleaned.upper())

    @property
    def required_files(self) -> tuple[Path, ...]:
        return (
            self.config.encoder,
            self.config.decoder,
            self.config.joiner,
            self.config.tokens,
            self.config.keywords_file,
        )

    def health(self) -> WakeWordHealth:
        missing = tuple(path.name for path in self.required_files if not path.is_file())
        if missing:
            return WakeWordHealth(
                "UNAVAILABLE",
                "sherpa-onnx-kws",
                self.config.model_root.name,
                missing,
                "WAKE_MODEL_UNAVAILABLE",
            )
        return WakeWordHealth(
            "AVAILABLE" if self._spotter is not None else "READY_TO_LOAD",
            "sherpa-onnx-kws",
            self.config.model_root.name,
            normalized_error=self._last_error,
        )

    def _load(self) -> None:
        if self._spotter is not None:
            return
        health = self.health()
        if health.missing_files:
            self._last_error = "WAKE_MODEL_UNAVAILABLE"
            raise RuntimeError(self._last_error)
        try:
            sherpa = import_module("sherpa_onnx")
            self._spotter = sherpa.KeywordSpotter(
                tokens=str(self.config.tokens),
                encoder=str(self.config.encoder),
                decoder=str(self.config.decoder),
                joiner=str(self.config.joiner),
                num_threads=self.config.num_threads,
                keywords_file=str(self.config.keywords_file),
                provider=self.config.provider,
            )
            self._last_error = None
        except (ImportError, ModuleNotFoundError):
            self._last_error = "WAKE_BACKEND_UNAVAILABLE"
            raise RuntimeError(self._last_error) from None
        except (AttributeError, OSError, RuntimeError, ValueError, TypeError):
            self._last_error = "WAKE_MODEL_LOAD_FAILED"
            raise RuntimeError(self._last_error) from None

    def suppress_for(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("suppression duration cannot be negative")
        self._suppressed_until = max(self._suppressed_until, monotonic() + seconds)

    def reset(self) -> None:
        self._last_detection = float("-inf")
        self._suppressed_until = float("-inf")

    def close(self) -> None:
        self._spotter = None

    def _window_is_eligible(self, frames: tuple[AudioFrame, ...]) -> bool:
        if not frames:
            return False
        samples = [sample for frame in frames for sample in frame.samples]
        if not samples:
            return False
        if len(samples) > int(16000 * self.config.maximum_window_seconds):
            samples = samples[-int(16000 * self.config.maximum_window_seconds) :]
        energy = (sum(sample * sample for sample in samples) / len(samples)) ** 0.5
        return energy >= self.config.minimum_energy

    def detect(self, frames: tuple[AudioFrame, ...], session_id: str) -> WakeDetection | None:
        now = monotonic()
        if now < self._suppressed_until:
            return None
        if now - self._last_detection < self.config.cooldown_seconds:
            return None
        if not self._window_is_eligible(frames):
            return None

        self._load()
        assert self._spotter is not None
        samples = [sample for frame in frames for sample in frame.samples]
        samples = samples[-int(16000 * self.config.maximum_window_seconds) :]

        try:
            stream = self._spotter.create_stream()
            stream.accept_waveform(16000, samples)
            # A short zero tail allows the streaming decoder to flush a phrase
            # that ends at the edge of the ring-buffer window.
            stream.accept_waveform(16000, [0.0] * int(0.24 * 16000))
            stream.input_finished()
            result = ""
            while self._spotter.is_ready(stream):
                self._spotter.decode_stream(stream)
                candidate = str(self._spotter.get_result(stream)).strip()
                if candidate:
                    result = candidate
                    self._spotter.reset_stream(stream)
                    break
        except (AttributeError, OSError, RuntimeError, ValueError, TypeError):
            self._last_error = "WAKE_INFERENCE_FAILED"
            return None

        label = self.normalize_label(result)
        allowed = {self.normalize_label(item) for item in self.config.accepted_labels}
        if not result or label not in allowed:
            return None

        self._last_detection = now
        start = frames[0].timestamp
        detected = frames[-1].timestamp
        return WakeDetection(
            keyword=result,
            normalized_keyword="HEY PANGU" if label.startswith("HEY_") else "PANGU",
            start_timestamp=start,
            detection_timestamp=detected,
            score=1.0,
            threshold=1.0,
            engine_name="sherpa-onnx-open-vocabulary-kws",
            audio_session_id=session_id,
        )


def load_wake_word_config(root: Path, cooldown_seconds: float = 2.0) -> WakeWordConfig:
    model_root = root / "models" / "voice" / "wake" / "sherpa-kws"
    return WakeWordConfig(
        model_root=model_root,
        encoder=model_root / "encoder.onnx",
        decoder=model_root / "decoder.onnx",
        joiner=model_root / "joiner.onnx",
        tokens=model_root / "tokens.txt",
        keywords_file=model_root / "keywords.txt",
        cooldown_seconds=cooldown_seconds,
    )


def verify_model_manifest(path: Path) -> dict[str, object]:
    """Validate an optional manifest without trusting its file paths blindly."""
    data = json.loads(path.read_text(encoding="utf-8"))
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("WAKE_MANIFEST_INVALID")
    base = path.parent.resolve()
    verified: dict[str, bool] = {}
    for name, expected in artifacts.items():
        if not isinstance(name, str) or not isinstance(expected, str) or len(expected) != 64:
            raise ValueError("WAKE_MANIFEST_INVALID")
        target = (base / name).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            verified[name] = False
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        verified[name] = actual.casefold() == expected.casefold()
    return {"verified": verified, "all_verified": bool(verified) and all(verified.values())}
