from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from time import monotonic
from typing import TYPE_CHECKING

from .events import EventBus, EventEnvelope, EventPriority
from .production_voice import ProductionVoiceSessionRuntime
from .tts import SpeechOutputProvider
from .voice import AudioFrame, SpeechSegment, SpeechSegmentController, VadConfiguration, VoiceState

if TYPE_CHECKING:
    from .runtime import Runtime


@dataclass(frozen=True)
class RealtimeTurnMetrics:
    wake_to_speech_ms: float
    speech_to_transcript_ms: float
    transcript_to_result_ms: float
    result_to_speech_ms: float
    total_turn_ms: float


class RealtimeVoiceTurnCoordinator:
    """Own one bounded post-wake conversational turn using existing runtime owners."""

    def __init__(
        self,
        voice: ProductionVoiceSessionRuntime,
        runtime: Runtime,
        events: EventBus,
        speaker: SpeechOutputProvider,
    ) -> None:
        self.voice = voice
        self.runtime = runtime
        self.events = events
        self.speaker = speaker
        self._turn_task: asyncio.Task[None] | None = None
        self._running = False
        self.completed_turns = 0
        self.failed_turns = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.events.subscribe("voice.wake.detected", self._on_wake)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self.events.unsubscribe("voice.wake.detected", self._on_wake)
        if self._turn_task is not None:
            self._turn_task.cancel()
            try:
                await self._turn_task
            except asyncio.CancelledError:
                pass
            self._turn_task = None
        await self.speaker.interrupt()

    async def _on_wake(self, event: EventEnvelope) -> None:
        if not self._running:
            return
        if self._turn_task is not None and not self._turn_task.done():
            await self.events.publish(
                EventEnvelope(
                    "voice.wake.suppressed",
                    {"reason": "TURN_ALREADY_ACTIVE", "session_id": self.voice.session_id},
                    EventPriority.LOW,
                )
            )
            return
        self._turn_task = asyncio.create_task(self._run_turn(event), name="pangu-realtime-turn")

    async def _capture_command(self) -> tuple[SpeechSegment | None, float]:
        started = monotonic()
        config = VadConfiguration(
            sample_rate=self.voice.config.sample_rate,
            window_size=self.voice.config.window_size,
            speech_threshold=self.voice.config.vad_threshold,
            minimum_speech_ms=self.voice.config.minimum_speech_ms,
            minimum_silence_ms=self.voice.config.end_silence_ms,
            prefix_padding_ms=self.voice.config.prefix_padding_ms,
            trailing_padding_ms=200,
            maximum_utterance_seconds=self.voice.config.maximum_command_seconds,
        )
        controller = SpeechSegmentController(config)
        last_sequence = 0
        deadline = started + self.voice.config.maximum_command_seconds
        first_speech_at: float | None = None
        while self._running and monotonic() < deadline:
            frames = self.voice.frames.recent(3000)
            fresh = [frame for frame in frames if frame.sequence > last_sequence]
            for frame in fresh:
                last_sequence = max(last_sequence, frame.sequence)
                activity = self.voice.vad.analyze(frame.samples, config.sample_rate, frame.timestamp)
                if activity.is_speech and first_speech_at is None:
                    first_speech_at = monotonic()
                segment = controller.process(frame, activity, self.voice.session_id)
                if segment is not None:
                    delay = ((first_speech_at or monotonic()) - started) * 1000
                    return segment, delay
            await asyncio.sleep(0.025)
        terminal = controller.end_of_file()
        delay = ((first_speech_at or monotonic()) - started) * 1000
        return terminal, delay

    async def _run_turn(self, wake_event: EventEnvelope) -> None:
        turn_started = monotonic()
        try:
            if self.voice.state != VoiceState.COMMAND_LISTENING:
                return
            await self.events.publish(
                EventEnvelope(
                    "voice.command.capture.started",
                    {"session_id": self.voice.session_id, "wake_event_id": wake_event.event_id},
                )
            )
            segment, wake_to_speech_ms = await self._capture_command()
            if segment is None:
                self.failed_turns += 1
                await self.voice.finish_turn("NO_COMMAND_SPEECH")
                await self._speak_and_return("I didn't catch a command.")
                return

            await self.voice.begin_transcription()
            speech_ended = monotonic()
            transcription = await asyncio.to_thread(
                self.voice.transcriber.transcribe,
                (AudioFrame(tuple(segment.samples), segment.start_timestamp, 1),),
            )
            speech_to_transcript_ms = (monotonic() - speech_ended) * 1000
            segment.clear_samples()
            if transcription.normalized_error or not transcription.normalized_transcript.strip():
                self.failed_turns += 1
                await self.events.publish(
                    EventEnvelope(
                        "voice.transcription.failed",
                        {
                            "session_id": self.voice.session_id,
                            "normalized_error": transcription.normalized_error or "EMPTY_TRANSCRIPT",
                        },
                    )
                )
                await self.voice.mark_command_ready()
                await self._speak_and_return("I couldn't understand that clearly.")
                return

            await self.events.publish(
                EventEnvelope(
                    "voice.transcription.completed",
                    {
                        "session_id": self.voice.session_id,
                        "text": transcription.normalized_transcript,
                        "language": transcription.detected_language,
                        "language_probability": transcription.language_probability,
                        "latency_ms": transcription.inference_latency_ms,
                    },
                )
            )
            transcript_ready = monotonic()
            result = await asyncio.to_thread(
                self.runtime.command, transcription.normalized_transcript, "voice"
            )
            transcript_to_result_ms = (monotonic() - transcript_ready) * 1000
            await self.voice.mark_command_ready()
            result_ready = monotonic()
            await self._speak_and_return(result.message)
            result_to_speech_ms = (monotonic() - result_ready) * 1000
            self.completed_turns += 1
            metrics = RealtimeTurnMetrics(
                wake_to_speech_ms,
                speech_to_transcript_ms,
                transcript_to_result_ms,
                result_to_speech_ms,
                (monotonic() - turn_started) * 1000,
            )
            await self.events.publish(
                EventEnvelope(
                    "voice.turn.completed",
                    {"session_id": self.voice.session_id, **asdict(metrics)},
                    EventPriority.LOW,
                )
            )
        except asyncio.CancelledError:
            raise
        except (RuntimeError, ValueError, OSError):
            self.failed_turns += 1
            await self.voice.fail_and_return_to_wake("REALTIME_TURN_FAILED")

    async def _speak_and_return(self, text: str) -> None:
        expected_seconds = max(1.0, min(15.0, len(text.split()) / 2.5 + 0.75))
        self.voice.suppress_wake_during_tts(expected_seconds)
        await self.events.publish(
            EventEnvelope("voice.response.started", {"session_id": self.voice.session_id})
        )
        output = await self.speaker.speak(text)
        await self.events.publish(
            EventEnvelope(
                "voice.response.completed",
                {
                    "session_id": self.voice.session_id,
                    "provider": output.provider,
                    "verification_state": output.verification_state,
                    "interrupted": output.interrupted,
                    "normalized_error": output.normalized_error,
                },
            )
        )
        await self.voice.return_to_wake()
