from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from time import monotonic
from typing import Any

import numpy as np

from .personalized_wake import PersonalizedWakeWordVerifier
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
    personalized_profile: Path | None = None
    speaker_model: Path | None = None
    owner_only: bool = False


@dataclass(frozen=True)
class WakeWordHealth:
    status: str
    backend: str
    model_root: str
    missing_files: tuple[str, ...] = ()
    normalized_error: str | None = None


class SherpaKeywordSpotterWakeWordEngine:
    """Local owner-personalized wake detector with sherpa KWS compatibility fallback.

    Production configuration enables the personalized path. A wake then requires two
    independent local checks: the enrolled acoustic pronunciation template must match
    "Hey Pangu", and the sherpa speaker embedding must match the enrolled owner voice.
    Raw enrollment audio is never required at runtime and is not persisted by PANGU.

    Direct unit/test configurations that do not provide personalized paths retain the
    original sherpa open-vocabulary keyword spotter behavior.
    """

    def __init__(self, config: WakeWordConfig) -> None:
        self.config = config
        self._spotter: Any | None = None
        self._personalized: PersonalizedWakeWordVerifier | None = None
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

    @property
    def personalized_enabled(self) -> bool:
        return (
            self.config.personalized_profile is not None and self.config.speaker_model is not None
        )

    def _personalized_verifier(self) -> PersonalizedWakeWordVerifier:
        if not self.personalized_enabled:
            raise RuntimeError("PERSONALIZED_WAKE_NOT_CONFIGURED")
        if self._personalized is None:
            assert self.config.personalized_profile is not None
            assert self.config.speaker_model is not None
            self._personalized = PersonalizedWakeWordVerifier(
                self.config.personalized_profile,
                self.config.speaker_model,
            )
        return self._personalized

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
        if self.personalized_enabled:
            personalized = self._personalized_verifier().health()
            if personalized.available:
                return WakeWordHealth(
                    "AVAILABLE",
                    "personalized-acoustic+speaker-verification",
                    Path(personalized.profile_path).name,
                )
            if self.config.owner_only:
                return WakeWordHealth(
                    "UNAVAILABLE",
                    "personalized-acoustic+speaker-verification",
                    Path(personalized.profile_path).name,
                    normalized_error=personalized.normalized_error,
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
        missing = tuple(path.name for path in self.required_files if not path.is_file())
        if missing:
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
        self._personalized = None

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

    def _personalized_detect(
        self, frames: tuple[AudioFrame, ...], session_id: str, now: float
    ) -> WakeDetection | None:
        verifier = self._personalized_verifier()
        health = verifier.health()
        if not health.available:
            self._last_error = health.normalized_error
            if self.config.owner_only:
                raise RuntimeError(health.normalized_error or "PERSONALIZED_WAKE_UNAVAILABLE")
            return None
        samples = np.asarray(
            [sample for frame in frames for sample in frame.samples],
            dtype=np.float32,
        )
        limit = int(16000 * self.config.maximum_window_seconds)
        if samples.size > limit:
            samples = samples[-limit:]
        match = verifier.verify(samples, 16000)
        if match is None:
            return None
        self._last_detection = now
        self._last_error = None
        combined_score = min(match.score, match.speaker_similarity)
        return WakeDetection(
            keyword="HEY_PANGU",
            normalized_keyword="HEY PANGU",
            start_timestamp=frames[0].timestamp,
            detection_timestamp=frames[-1].timestamp,
            score=combined_score,
            threshold=combined_score,
            engine_name="pangu-personalized-owner-wake-v1",
            audio_session_id=session_id,
        )

    def detect(self, frames: tuple[AudioFrame, ...], session_id: str) -> WakeDetection | None:
        now = monotonic()
        if now < self._suppressed_until:
            return None
        if now - self._last_detection < self.config.cooldown_seconds:
            return None
        if not self._window_is_eligible(frames):
            return None

        if self.personalized_enabled:
            personalized = self._personalized_detect(frames, session_id, now)
            if personalized is not None or self.config.owner_only:
                return personalized

        self._load()
        assert self._spotter is not None
        samples = [sample for frame in frames for sample in frame.samples]
        samples = samples[-int(16000 * self.config.maximum_window_seconds) :]

        try:
            stream = self._spotter.create_stream()
            stream.accept_waveform(16000, samples)
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
        personalized_profile=root / "runtime-data" / "identity" / "owner_wake_profile.json",
        speaker_model=(
            root
            / "models"
            / "voice"
            / "speaker"
            / "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
        ),
        owner_only=True,
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
