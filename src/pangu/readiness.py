from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from .settings import PanguSettings


class ReadinessState(StrEnum):
    READY = "READY"
    OPTIONAL = "OPTIONAL"
    MISSING = "MISSING"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    state: ReadinessState
    detail: str
    action: str | None = None


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    checks: tuple[ReadinessCheck, ...]

    def public(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "checks": [asdict(item) for item in self.checks],
        }


class PanguReadinessInspector:
    """Truthful target-machine preflight; no network calls and no secret disclosure."""

    def __init__(self, root: Path, settings: PanguSettings | None = None) -> None:
        self.root = root.resolve()
        self.settings = settings or PanguSettings.load_root(self.root)

    @staticmethod
    def _module(name: str) -> bool:
        return importlib.util.find_spec(name) is not None

    def _file(
        self,
        name: str,
        path: Path,
        *,
        required: bool,
        action: str,
    ) -> ReadinessCheck:
        if path.exists():
            return ReadinessCheck(name, ReadinessState.READY, str(path))
        return ReadinessCheck(
            name,
            ReadinessState.MISSING if required else ReadinessState.OPTIONAL,
            f"Not found: {path}",
            action,
        )

    def _artifact_set(
        self,
        name: str,
        directory: Path,
        required_files: tuple[str, ...],
        *,
        action: str,
    ) -> ReadinessCheck:
        missing = [item for item in required_files if not (directory / item).is_file()]
        if not missing:
            return ReadinessCheck(name, ReadinessState.READY, str(directory))
        detail = f"Incomplete: {directory}; missing: {', '.join(missing)}"
        return ReadinessCheck(name, ReadinessState.MISSING, detail, action)

    def inspect(self) -> ReadinessReport:
        checks: list[ReadinessCheck] = []
        supported_python = sys.version_info >= (3, 12) and sys.version_info < (3, 15)
        checks.append(
            ReadinessCheck(
                "python",
                ReadinessState.READY if supported_python else ReadinessState.BLOCKED,
                f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                None if supported_python else "Install Python 3.12, 3.13, or 3.14.",
            )
        )

        checks.append(
            ReadinessCheck(
                "gemini_api",
                ReadinessState.READY if self.settings.gemini_api_key else ReadinessState.MISSING,
                "Gemini API key configured."
                if self.settings.gemini_api_key
                else "Gemini API key is not configured.",
                None
                if self.settings.gemini_api_key
                else "Set GEMINI_API_KEY in the repository .env file.",
            )
        )

        checks.append(
            self._artifact_set(
                "whisper_model",
                self.root / "models" / "voice" / "whisper",
                ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"),
                action="Install the configured local Faster Whisper model under models/voice/whisper.",
            )
        )
        checks.append(
            self._artifact_set(
                "wake_model",
                self.root / "models" / "voice" / "wake" / "sherpa-kws",
                (
                    "encoder.onnx",
                    "decoder.onnx",
                    "joiner.onnx",
                    "tokens.txt",
                    "keywords.txt",
                    "en.phone",
                    "manifest.json",
                ),
                action="Run scripts/install-wake-model.ps1 with the trusted model archive SHA-256.",
            )
        )

        if self.settings.pangu_gestures_enabled:
            checks.append(
                self._file(
                    "gesture_model",
                    self.root / self.settings.pangu_gesture_model_path,
                    required=True,
                    action="Install the MediaPipe Hand Landmarker model configured by PANGU_GESTURE_MODEL_PATH.",
                )
            )
            checks.append(
                ReadinessCheck(
                    "mediapipe",
                    ReadinessState.READY if self._module("mediapipe") else ReadinessState.MISSING,
                    "MediaPipe installed."
                    if self._module("mediapipe")
                    else "MediaPipe is not installed.",
                    None if self._module("mediapipe") else "Install PANGU with the vision extra.",
                )
            )

        if self.settings.pangu_screen_observation_ocr_enabled:
            tesseract = shutil.which("tesseract")
            checks.append(
                ReadinessCheck(
                    "tesseract",
                    ReadinessState.READY if tesseract else ReadinessState.MISSING,
                    tesseract or "Tesseract executable is not on PATH.",
                    None if tesseract else "Install Tesseract OCR and add it to PATH.",
                )
            )

        desktop_required = self.settings.pangu_computer_use_enabled
        pywinauto = self._module("pywinauto")
        checks.append(
            ReadinessCheck(
                "desktop_automation",
                ReadinessState.READY
                if pywinauto
                else ReadinessState.MISSING
                if desktop_required
                else ReadinessState.OPTIONAL,
                "pywinauto installed." if pywinauto else "pywinauto not installed.",
                None
                if pywinauto
                else "Install PANGU with the desktop extra before enabling computer use.",
            )
        )

        browser_required = self.settings.pangu_browser_enabled or self.settings.pangu_media_enabled
        playwright = self._module("playwright")
        checks.append(
            ReadinessCheck(
                "playwright",
                ReadinessState.READY
                if playwright
                else ReadinessState.MISSING
                if browser_required
                else ReadinessState.OPTIONAL,
                "Playwright Python package installed."
                if playwright
                else "Playwright is not installed.",
                None if playwright else "Install PANGU with the browser extra.",
            )
        )
        chromium_candidates = (
            Path.home() / "AppData" / "Local" / "ms-playwright",
            Path.home() / ".cache" / "ms-playwright",
        )
        chromium_installed = any(path.exists() for path in chromium_candidates)
        checks.append(
            ReadinessCheck(
                "playwright_chromium",
                ReadinessState.READY
                if chromium_installed
                else ReadinessState.MISSING
                if browser_required
                else ReadinessState.OPTIONAL,
                "Playwright browser cache found."
                if chromium_installed
                else "Playwright Chromium cache was not found.",
                None if chromium_installed else "Run: python -m playwright install chromium",
            )
        )

        if self.settings.pangu_phone_enabled:
            configured = self.settings.pangu_phone_pairing_secret is not None
            checks.append(
                ReadinessCheck(
                    "phone_pairing_secret",
                    ReadinessState.READY if configured else ReadinessState.BLOCKED,
                    "Phone pairing secret configured."
                    if configured
                    else "Phone integration is enabled but no valid pairing secret is configured.",
                    None
                    if configured
                    else "Set a random PANGU_PHONE_PAIRING_SECRET of at least 32 characters on PANGU and the companion.",
                )
            )
        else:
            checks.append(
                ReadinessCheck(
                    "phone_integration",
                    ReadinessState.OPTIONAL,
                    "Phone integration is disabled.",
                    "Enable PANGU_PHONE_ENABLED after installing and pairing the Android companion.",
                )
            )

        blocked = {ReadinessState.MISSING, ReadinessState.BLOCKED}
        ready = not any(item.state in blocked for item in checks)
        return ReadinessReport(ready, tuple(checks))
