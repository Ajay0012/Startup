from __future__ import annotations

import asyncio
from queue import Full
from threading import Event, Thread
from time import sleep

from .events import EventEnvelope
from .voice import AudioFrame, VoiceOutcome, VoiceSessionRuntime, VoiceState, WakeDetection


class WakePhrasePolicyVerifier:
    """Second-stage policy check after local KWS; never transcribes pre-wake audio."""

    _accepted = {"pangu", "hey pangu", "hay pangu", "hey panguu", "hey pangoo"}

    def verify(self, frames: tuple[AudioFrame, ...], phrase: str) -> VoiceOutcome:
        normalized = " ".join(phrase.casefold().replace("_", " ").split())
        return VoiceOutcome.CONFIRMED if normalized in self._accepted else VoiceOutcome.REJECTED


class ProductionVoiceSessionRuntime(VoiceSessionRuntime):
    """Always-on production voice runtime using the existing single voice lifecycle."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._wake_worker: Thread | None = None
        self._wake_stop = Event()
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._wake_detection_in_flight = False
        self.metrics["wake_inference_errors"] = 0
        self.metrics["wake_suppressed"] = 0
        self.metrics["wake_buffer_clears"] = 0

    def _start_wake_worker(self) -> None:
        self._wake_stop.clear()

        def run() -> None:
            while not self._wake_stop.is_set():
                if self.state != VoiceState.IDLE_LISTENING or self._wake_detection_in_flight:
                    sleep(0.04)
                    continue
                frames = self.frames.recent(3000)
                if not frames:
                    sleep(0.04)
                    continue
                try:
                    detection = self.wake.detect(frames, self.session_id)
                except RuntimeError:
                    self.metrics["wake_inference_errors"] += 1
                    sleep(0.15)
                    continue
                if detection is None:
                    sleep(0.04)
                    continue
                self._wake_detection_in_flight = True
                loop = self._runtime_loop
                if loop is not None and loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(self._handle_wake(detection), loop)
                    try:
                        future.result(timeout=2.0)
                    except Exception:
                        self.metrics["wake_inference_errors"] += 1
                    finally:
                        self._wake_detection_in_flight = False
                else:
                    self._wake_detection_in_flight = False
                sleep(0.04)

        self._wake_worker = Thread(target=run, name="pangu-voice-wake", daemon=True)
        self._wake_worker.start()

    async def _handle_wake(self, detection: WakeDetection) -> None:
        if self.state != VoiceState.IDLE_LISTENING:
            self.metrics["wake_suppressed"] += 1
            return
        self.metrics["wake_candidates"] += 1
        await self._transition(VoiceState.SPEECH_CANDIDATE, "voice.speech.candidate")
        await self._transition(VoiceState.WAKE_CANDIDATE, "voice.wake.candidate")
        policy = self.verifier.verify(self.frames.recent(3000), detection.normalized_keyword)
        if policy != VoiceOutcome.CONFIRMED:
            self.metrics["wake_rejections"] += 1
            await self._transition(VoiceState.COOLDOWN, "voice.wake.rejected")
            await self._transition(VoiceState.IDLE_LISTENING, "voice.wake.listening")
            return
        self.metrics["wake_confirmations"] += 1
        await self._transition(VoiceState.WAKE_CONFIRMED, "voice.wake.confirmed")
        # Never let pre-wake/background audio leak into command capture.
        self.frames.clear()
        self.metrics["wake_buffer_clears"] += 1
        await self._transition(VoiceState.COMMAND_LISTENING, "voice.command.listening")
        await self.events.publish(
            EventEnvelope(
                "voice.wake.detected",
                {
                    "session_id": self.session_id,
                    "keyword": detection.normalized_keyword,
                    "engine": detection.engine_name,
                    "detected_at": detection.detection_timestamp,
                    "stale_buffer_cleared": True,
                },
            )
        )

    async def begin_transcription(self) -> None:
        if self.state == VoiceState.COMMAND_LISTENING:
            await self._transition(VoiceState.TURN_ENDING, "voice.speech.ended")
        if self.state == VoiceState.TURN_ENDING:
            await self._transition(VoiceState.TRANSCRIBING, "voice.transcription.started")

    async def mark_command_ready(self) -> None:
        if self.state == VoiceState.TRANSCRIBING:
            await self._transition(VoiceState.COMMAND_READY, "voice.command.ready")

    async def finish_turn(self, reason: str) -> None:
        await self.begin_transcription()
        await self.mark_command_ready()
        await self.events.publish(
            EventEnvelope("voice.turn.ended", {"session_id": self.session_id, "reason": reason})
        )

    async def return_to_wake(self) -> None:
        if self.state == VoiceState.COMMAND_READY:
            await self._transition(VoiceState.COOLDOWN, "voice.cooldown.started")
        if self.state == VoiceState.COOLDOWN:
            await self._transition(VoiceState.IDLE_LISTENING, "voice.wake.listening")

    async def fail_and_return_to_wake(self, reason: str) -> None:
        if self.state == VoiceState.COMMAND_LISTENING:
            await self.finish_turn(reason)
        elif self.state == VoiceState.TURN_ENDING:
            await self.begin_transcription()
            await self.mark_command_ready()
        elif self.state == VoiceState.TRANSCRIBING:
            await self.mark_command_ready()
        await self.events.publish(
            EventEnvelope("voice.turn.failed", {"session_id": self.session_id, "reason": reason})
        )
        await self.return_to_wake()

    async def start(self, selector: str | None = None, capture: bool = True) -> None:
        self._runtime_loop = asyncio.get_running_loop()
        await self._transition(VoiceState.INITIALIZING, "voice.runtime.started")
        selection = self.select_device(selector)
        if selection.device is None:
            await self._transition(VoiceState.DEVICE_UNAVAILABLE, "voice.device.disconnected")
            return
        self.session_id = __import__("uuid").uuid4().hex
        if capture:
            self._accepting_frames = True
            self._start_worker()
            self.input.start(selection.device.selector, self.accept_callback_frame)
            self.metrics["stream_start_count"] += 1
        await self._transition(VoiceState.IDLE_LISTENING, "voice.device.selected")
        if capture:
            self._start_wake_worker()

    async def stop(self) -> None:
        if self.state == VoiceState.STOPPED:
            return
        if self.state != VoiceState.SHUTTING_DOWN:
            await self._transition(VoiceState.SHUTTING_DOWN, "voice.runtime.stopped")
        self._accepting_frames = False
        self._wake_stop.set()
        try:
            self.input.stop()
        finally:
            if self._wake_worker is not None:
                self._wake_worker.join(timeout=2.0)
                self._wake_worker = None
            try:
                self.queue.put_nowait(None)
            except Full:
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except Exception:
                    pass
                self.queue.put_nowait(None)
            if self._worker is not None:
                self._worker.join(timeout=2.0)
                self._worker = None
            self.frames.clear()
            self.vad.reset()
            self.metrics["stream_stop_count"] += 1
            wake_reset = getattr(self.wake, "reset", None)
            if callable(wake_reset):
                wake_reset()
            wake_close = getattr(self.wake, "close", None)
            if callable(wake_close):
                wake_close()
            self._runtime_loop = None
        await self._transition(VoiceState.STOPPED, "voice.runtime.stopped")

    def suppress_wake_during_tts(self, seconds: float) -> None:
        suppress = getattr(self.wake, "suppress_for", None)
        if callable(suppress):
            suppress(seconds)
            self.metrics["wake_suppressed"] += 1
