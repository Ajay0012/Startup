from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from time import monotonic
from typing import TYPE_CHECKING

from .events import EventBus, EventEnvelope, EventPriority
from .production_voice import ProductionVoiceSessionRuntime
from .tts import SpeechOutputProvider, SpeechOutputResult
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
    conversational_turns: int = 1


@dataclass(frozen=True)
class BargeInPolicy:
    guard_seconds: float = 0.45
    minimum_speech_ms: float = 320.0
    absolute_energy_floor: float = 0.035
    echo_multiplier: float = 1.7
    maximum_followups: int = 3

    def __post_init__(self) -> None:
        if not 0.1 <= self.guard_seconds <= 2:
            raise ValueError("barge-in guard must be between 0.1 and 2 seconds")
        if not 100 <= self.minimum_speech_ms <= 1500:
            raise ValueError("barge-in speech duration is invalid")
        if not 0 <= self.absolute_energy_floor <= 1 or not 1 <= self.echo_multiplier <= 5:
            raise ValueError("barge-in energy policy is invalid")
        if not 0 <= self.maximum_followups <= 10:
            raise ValueError("barge-in followup limit is invalid")


class RealtimeVoiceTurnCoordinator:
    """Own bounded wake-to-response turns, including guarded conversational barge-in."""

    def __init__(
        self,
        voice: ProductionVoiceSessionRuntime,
        runtime: Runtime,
        events: EventBus,
        speaker: SpeechOutputProvider,
        barge_in: BargeInPolicy | None = None,
    ) -> None:
        self.voice = voice
        self.runtime = runtime
        self.events = events
        self.speaker = speaker
        self.barge_in = barge_in or BargeInPolicy()
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

    async def _capture_command(
        self, minimum_sequence: int = 0
    ) -> tuple[SpeechSegment | None, float]:
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
        last_sequence = minimum_sequence
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

    async def _watch_for_barge_in(
        self,
        speech_task: asyncio.Task[SpeechOutputResult],
        initial_sequence: int,
    ) -> int | None:
        guard_until = monotonic() + self.barge_in.guard_seconds
        last_sequence = initial_sequence
        echo_baseline = 0.0
        speech_ms = 0.0
        candidate_start = initial_sequence
        while self._running and not speech_task.done():
            fresh = [
                frame
                for frame in self.voice.frames.recent(1200)
                if frame.sequence > last_sequence
            ]
            for frame in fresh:
                last_sequence = max(last_sequence, frame.sequence)
                activity = self.voice.vad.analyze(
                    frame.samples, self.voice.config.sample_rate, frame.timestamp
                )
                if monotonic() < guard_until:
                    echo_baseline = max(echo_baseline, activity.energy_level)
                    continue
                threshold = max(
                    self.barge_in.absolute_energy_floor,
                    echo_baseline * self.barge_in.echo_multiplier,
                )
                admitted = (
                    activity.is_speech
                    and activity.energy_gate_passed
                    and activity.energy_level >= threshold
                )
                if admitted:
                    if speech_ms == 0:
                        candidate_start = max(initial_sequence, frame.sequence - 1)
                    speech_ms += activity.frame_duration_ms
                    if speech_ms >= self.barge_in.minimum_speech_ms:
                        return candidate_start
                else:
                    speech_ms = 0.0
                    candidate_start = last_sequence
            await asyncio.sleep(0.02)
        return None

    async def _speak_with_barge_in(self, text: str) -> int | None:
        expected_seconds = max(1.0, min(15.0, len(text.split()) / 2.5 + 0.75))
        self.voice.suppress_wake_during_tts(expected_seconds)
        self.voice.frames.clear()
        self.voice.vad.reset()
        initial_sequence = self.voice._sequence
        await self.events.publish(
            EventEnvelope("voice.response.started", {"session_id": self.voice.session_id})
        )
        speech_task = asyncio.create_task(self.speaker.speak(text), name="pangu-tts")
        barge_task = asyncio.create_task(
            self._watch_for_barge_in(speech_task, initial_sequence),
            name="pangu-barge-in",
        )
        await asyncio.wait({speech_task, barge_task}, return_when=asyncio.FIRST_COMPLETED)
        barge_sequence = barge_task.result() if barge_task.done() else None
        if barge_sequence is not None and not speech_task.done():
            await self.speaker.interrupt()
        output = await speech_task
        if not barge_task.done():
            barge_task.cancel()
            try:
                await barge_task
            except asyncio.CancelledError:
                pass
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
        if barge_sequence is not None:
            await self.events.publish(
                EventEnvelope(
                    "voice.barge_in.detected",
                    {
                        "session_id": self.voice.session_id,
                        "sequence": barge_sequence,
                        "tts_interrupted": output.interrupted,
                    },
                )
            )
        return barge_sequence

    async def _run_turn(self, wake_event: EventEnvelope) -> None:
        turn_started = monotonic()
        minimum_sequence = 0
        wake_to_speech_ms = 0.0
        speech_to_transcript_ms = 0.0
        transcript_to_result_ms = 0.0
        result_to_speech_ms = 0.0
        conversational_turns = 0
        try:
            for followup in range(self.barge_in.maximum_followups + 1):
                if self.voice.state != VoiceState.COMMAND_LISTENING:
                    return
                conversational_turns += 1
                await self.events.publish(
                    EventEnvelope(
                        "voice.command.capture.started",
                        {
                            "session_id": self.voice.session_id,
                            "wake_event_id": wake_event.event_id,
                            "conversational_turn": conversational_turns,
                        },
                    )
                )
                segment, wake_to_speech_ms = await self._capture_command(minimum_sequence)
                if segment is None:
                    self.failed_turns += 1
                    await self.voice.finish_turn("NO_COMMAND_SPEECH")
                    barge_sequence = await self._speak_with_barge_in(
                        "I didn't catch a command."
                    )
                    if barge_sequence is not None and followup < self.barge_in.maximum_followups:
                        await self.voice.begin_barge_in()
                        self.voice.vad.reset()
                        minimum_sequence = barge_sequence
                        continue
                    await self.voice.return_to_wake()
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
                                "normalized_error": (
                                    transcription.normalized_error or "EMPTY_TRANSCRIPT"
                                ),
                            },
                        )
                    )
                    await self.voice.mark_command_ready()
                    barge_sequence = await self._speak_with_barge_in(
                        "I couldn't understand that clearly."
                    )
                    if barge_sequence is not None and followup < self.barge_in.maximum_followups:
                        await self.voice.begin_barge_in()
                        self.voice.vad.reset()
                        minimum_sequence = barge_sequence
                        continue
                    await self.voice.return_to_wake()
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
                barge_sequence = await self._speak_with_barge_in(result.message)
                result_to_speech_ms = (monotonic() - result_ready) * 1000
                if barge_sequence is not None and followup < self.barge_in.maximum_followups:
                    await self.voice.begin_barge_in()
                    self.voice.vad.reset()
                    minimum_sequence = barge_sequence
                    continue

                self.voice.frames.clear()
                self.voice.vad.reset()
                await self.voice.return_to_wake()
                self.completed_turns += 1
                metrics = RealtimeTurnMetrics(
                    wake_to_speech_ms,
                    speech_to_transcript_ms,
                    transcript_to_result_ms,
                    result_to_speech_ms,
                    (monotonic() - turn_started) * 1000,
                    conversational_turns,
                )
                await self.events.publish(
                    EventEnvelope(
                        "voice.turn.completed",
                        {"session_id": self.voice.session_id, **asdict(metrics)},
                        EventPriority.LOW,
                    )
                )
                return
        except asyncio.CancelledError:
            raise
        except (RuntimeError, ValueError, OSError):
            self.failed_turns += 1
            await self.voice.fail_and_return_to_wake("REALTIME_TURN_FAILED")
