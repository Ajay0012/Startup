from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import import_module
from threading import Lock
from time import monotonic
from typing import Protocol


@dataclass(frozen=True)
class SpeechOutputResult:
    text_length: int
    provider: str
    verification_state: str
    interrupted: bool = False
    normalized_error: str | None = None
    latency_ms: float = 0.0


class SpeechOutputProvider(Protocol):
    async def speak(self, text: str) -> SpeechOutputResult: ...
    async def interrupt(self) -> bool: ...


class WindowsSapiSpeechProvider:
    """Local Windows SAPI TTS with explicit interruption and no cloud dependency."""

    def __init__(self, rate: int = 0, volume: int = 100) -> None:
        if not -10 <= rate <= 10:
            raise ValueError("SAPI rate must be between -10 and 10")
        if not 0 <= volume <= 100:
            raise ValueError("SAPI volume must be between 0 and 100")
        self.rate = rate
        self.volume = volume
        self._voice: object | None = None
        self._lock = Lock()
        self._speaking = False
        self._interrupt_requested = False

    def _ensure_voice(self) -> object:
        with self._lock:
            if self._voice is not None:
                return self._voice
            client = import_module("comtypes.client")
            voice = client.CreateObject("SAPI.SpVoice", dynamic=True)
            voice.Rate = self.rate
            voice.Volume = self.volume
            self._voice = voice
            return voice

    def _speak_sync(self, text: str) -> SpeechOutputResult:
        started = monotonic()
        if not text.strip():
            return SpeechOutputResult(0, "windows-sapi", "UNVERIFIED", normalized_error="EMPTY_TEXT")
        try:
            voice = self._ensure_voice()
        except (ImportError, ModuleNotFoundError, OSError, RuntimeError):
            return SpeechOutputResult(
                len(text), "windows-sapi", "UNAVAILABLE", normalized_error="TTS_BACKEND_UNAVAILABLE"
            )
        self._interrupt_requested = False
        self._speaking = True
        interrupted = False
        try:
            # Async SAPI speak keeps the worker responsive enough for purge requests.
            voice.Speak(text, 1)
            while True:
                if self._interrupt_requested:
                    voice.Speak("", 3)  # SVSFlagsAsync | SVSFPurgeBeforeSpeak
                    interrupted = True
                    break
                if bool(voice.WaitUntilDone(50)):
                    break
        except (OSError, RuntimeError, ValueError):
            return SpeechOutputResult(
                len(text),
                "windows-sapi",
                "UNVERIFIED",
                interrupted=interrupted,
                normalized_error="TTS_SYNTHESIS_FAILED",
                latency_ms=(monotonic() - started) * 1000,
            )
        finally:
            self._speaking = False
        return SpeechOutputResult(
            len(text),
            "windows-sapi",
            "VERIFIED" if not interrupted else "INTERRUPTED",
            interrupted=interrupted,
            latency_ms=(monotonic() - started) * 1000,
        )

    async def speak(self, text: str) -> SpeechOutputResult:
        return await asyncio.to_thread(self._speak_sync, text)

    async def interrupt(self) -> bool:
        if not self._speaking:
            return False
        self._interrupt_requested = True
        return True


class NullSpeechProvider:
    """Explicit no-TTS provider for tests and text-only mode."""

    async def speak(self, text: str) -> SpeechOutputResult:
        return SpeechOutputResult(len(text), "none", "DISABLED")

    async def interrupt(self) -> bool:
        return False
