from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass
from time import monotonic
from typing import TYPE_CHECKING

from .conversation_intelligence import (
    ConversationAct,
    ConversationActClassifier,
    ConversationRepairState,
    PartialTranscriptStabilizer,
    SemanticEndOfTurnDetector,
)
from .events import EventBus, EventEnvelope, EventPriority
from .production_voice import ProductionVoiceSessionRuntime
from .realtime_voice import BargeInPolicy, RealtimeTurnMetrics, RealtimeVoiceTurnCoordinator
from .tts import SpeechOutputProvider
from .voice import AudioFrame, SpeechSegment, SpeechSegmentController, VadConfiguration, VoiceState

if TYPE_CHECKING:
    from .runtime import Runtime


@dataclass(frozen=True)
class FullDuplexPolicy:
    partial_interval_seconds: float = 0.45
    partial_window_seconds: float = 6.0
    semantic_minimum_silence_ms: float = 360.0
    maximum_partial_audio_seconds: float = 8.0
    response_chunk_words: int = 18

    def __post_init__(self) -> None:
        if not 0.2 <= self.partial_interval_seconds <= 2.0:
            raise ValueError("partial interval must be between 0.2 and 2 seconds")
        if not 2 <= self.partial_window_seconds <= 12:
            raise ValueError("partial window must be between 2 and 12 seconds")
        if not 200 <= self.semantic_minimum_silence_ms <= 1200:
            raise ValueError("semantic silence threshold is invalid")
        if not 2 <= self.maximum_partial_audio_seconds <= 15:
            raise ValueError("maximum partial audio is invalid")
        if not 6 <= self.response_chunk_words <= 40:
            raise ValueError("response chunk size is invalid")


class AdvancedRealtimeVoiceTurnCoordinator(RealtimeVoiceTurnCoordinator):
    """Enhance the existing voice owner with conversational full-duplex behavior.

    Faster Whisper is not a native streaming decoder, so partial hypotheses are generated
    from a bounded rolling in-memory window. Final command transcription still uses the
    complete accepted segment. No second microphone, recorder, EventBus, or voice lifecycle
    is created here.
    """

    def __init__(
        self,
        voice: ProductionVoiceSessionRuntime,
        runtime: Runtime,
        events: EventBus,
        speaker: SpeechOutputProvider,
        barge_in: BargeInPolicy | None = None,
        policy: FullDuplexPolicy | None = None,
    ) -> None:
        super().__init__(voice, runtime, events, speaker, barge_in)
        self.full_duplex = policy or FullDuplexPolicy()
        self.partial_stabilizer = PartialTranscriptStabilizer(stable_repetitions=2)
        self.turn_detector = SemanticEndOfTurnDetector()
        self.act_classifier = ConversationActClassifier()
        self.repair_state = ConversationRepairState()
        self._last_response_text: str | None = None
        self._pending_response_chunks: tuple[str, ...] = ()
        self._pending_chunk_index = 0

    @staticmethod
    def _chunk_response(text: str, maximum_words: int) -> tuple[str, ...]:
        words = text.split()
        if not words:
            return ()
        chunks: list[str] = []
        start = 0
        while start < len(words):
            end = min(len(words), start + maximum_words)
            if end < len(words):
                search_start = max(start + 4, end - 6)
                for index in range(end - 1, search_start - 1, -1):
                    if words[index].endswith((".", "?", "!", ";", ":")):
                        end = index + 1
                        break
            chunks.append(" ".join(words[start:end]))
            start = end
        return tuple(chunks)

    async def _partial_transcribe(self, frames: tuple[AudioFrame, ...]) -> str:
        if not frames:
            return ""
        result = await asyncio.to_thread(self.voice.transcriber.transcribe, frames)
        if result.normalized_error and not result.normalized_transcript:
            return ""
        hypothesis = self.partial_stabilizer.update(
            result.normalized_transcript,
            confidence=0.5 if result.normalized_transcript else 0.0,
            is_final=False,
        )
        await self.events.publish(
            EventEnvelope(
                "voice.transcription.partial",
                {
                    "session_id": self.voice.session_id,
                    "text": hypothesis.text,
                    "stable_prefix": hypothesis.stable_prefix,
                    "confidence": hypothesis.confidence,
                    "confidence_source": "rolling_stability_not_asr_probability",
                },
                EventPriority.LOW,
            )
        )
        return hypothesis.text

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
        partial_due = started + self.full_duplex.partial_interval_seconds
        partial_text = ""
        silence_ms = 0.0
        rolling: deque[AudioFrame] = deque()
        retained_samples = 0
        maximum_samples = int(
            self.voice.config.sample_rate
            * min(
                self.full_duplex.partial_window_seconds,
                self.full_duplex.maximum_partial_audio_seconds,
            )
        )
        self.partial_stabilizer.reset()

        while self._running and monotonic() < deadline:
            fresh = [
                frame for frame in self.voice.frames.recent(3000) if frame.sequence > last_sequence
            ]
            for frame in fresh:
                last_sequence = max(last_sequence, frame.sequence)
                activity = self.voice.vad.analyze(
                    frame.samples, config.sample_rate, frame.timestamp
                )
                if activity.is_speech and activity.energy_gate_passed:
                    if first_speech_at is None:
                        first_speech_at = monotonic()
                    silence_ms = 0.0
                elif first_speech_at is not None:
                    silence_ms += activity.frame_duration_ms

                if first_speech_at is not None:
                    rolling.append(frame)
                    retained_samples += len(frame.samples)
                    while rolling and retained_samples > maximum_samples:
                        removed = rolling.popleft()
                        retained_samples -= len(removed.samples)

                segment = controller.process(frame, activity, self.voice.session_id)
                if segment is not None:
                    delay = ((first_speech_at or monotonic()) - started) * 1000
                    return segment, delay

            now = monotonic()
            if first_speech_at is not None and rolling and now >= partial_due:
                partial_due = now + self.full_duplex.partial_interval_seconds
                partial_text = await self._partial_transcribe(tuple(rolling)) or partial_text

            if partial_text and silence_ms >= self.full_duplex.semantic_minimum_silence_ms:
                decision = self.turn_detector.decide(
                    partial_text,
                    silence_ms=silence_ms,
                    asr_final=False,
                    vad_confidence=1.0,
                )
                if decision.should_end:
                    await self.events.publish(
                        EventEnvelope(
                            "voice.turn.semantic_end",
                            {
                                "session_id": self.voice.session_id,
                                "score": decision.score,
                                "reason": decision.reason,
                            },
                            EventPriority.LOW,
                        )
                    )
                    terminal = controller.end_of_file()
                    delay = ((first_speech_at or monotonic()) - started) * 1000
                    return terminal, delay

            await asyncio.sleep(0.02)

        terminal = controller.end_of_file()
        delay = ((first_speech_at or monotonic()) - started) * 1000
        return terminal, delay

    async def _speak_chunks(self, chunks: tuple[str, ...], start_index: int = 0) -> int | None:
        for index in range(start_index, len(chunks)):
            chunk = chunks[index]
            self._pending_chunk_index = index
            await self.events.publish(
                EventEnvelope(
                    "voice.response.chunk",
                    {
                        "session_id": self.voice.session_id,
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                        "text": chunk,
                    },
                    EventPriority.LOW,
                )
            )
            barge_sequence = await super()._speak_with_barge_in(chunk)
            if barge_sequence is not None:
                self._pending_chunk_index = index
                return barge_sequence
        self._pending_response_chunks = ()
        self._pending_chunk_index = 0
        return None

    async def _speak_with_barge_in(self, text: str) -> int | None:
        chunks = self._chunk_response(text, self.full_duplex.response_chunk_words)
        if not chunks:
            return None
        self._last_response_text = text
        self._pending_response_chunks = chunks
        self._pending_chunk_index = 0
        return await self._speak_chunks(chunks)

    async def _continue_response(self) -> int | None:
        if not self._pending_response_chunks:
            return None
        return await self._speak_chunks(
            self._pending_response_chunks,
            self._pending_chunk_index,
        )

    async def classify_conversational_utterance(self, text: str) -> ConversationAct:
        act = self.act_classifier.classify(text)
        self.repair_state.apply(act, text)
        await self.events.publish(
            EventEnvelope(
                "voice.conversation.act",
                {
                    "session_id": self.voice.session_id,
                    "act": act.value,
                    "corrections": self.repair_state.corrections,
                },
                EventPriority.LOW,
            )
        )
        return act

    async def _continue_listening(self, minimum_sequence: int) -> int:
        await self.voice.mark_command_ready()
        await self.voice.begin_barge_in()
        self.voice.vad.reset()
        return max(minimum_sequence, self.voice._sequence)

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
                    barge_sequence = await self._speak_with_barge_in("I didn't catch that.")
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
                text = transcription.normalized_transcript.strip()
                if transcription.normalized_error or not text:
                    self.failed_turns += 1
                    await self.events.publish(
                        EventEnvelope(
                            "voice.transcription.failed",
                            {
                                "session_id": self.voice.session_id,
                                "normalized_error": transcription.normalized_error
                                or "EMPTY_TRANSCRIPT",
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

                self.partial_stabilizer.update(text, confidence=1.0, is_final=True)
                await self.events.publish(
                    EventEnvelope(
                        "voice.transcription.completed",
                        {
                            "session_id": self.voice.session_id,
                            "text": text,
                            "language": transcription.detected_language,
                            "language_probability": transcription.language_probability,
                            "latency_ms": transcription.inference_latency_ms,
                        },
                    )
                )
                act = await self.classify_conversational_utterance(text)

                if act == ConversationAct.CANCEL:
                    await self.voice.mark_command_ready()
                    await self.speaker.interrupt()
                    self._pending_response_chunks = ()
                    await self.voice.return_to_wake()
                    return

                if act == ConversationAct.BACKCHANNEL:
                    if followup < self.barge_in.maximum_followups:
                        minimum_sequence = await self._continue_listening(minimum_sequence)
                        continue
                    await self.voice.mark_command_ready()
                    await self.voice.return_to_wake()
                    return

                if act == ConversationAct.INCOMPLETE:
                    await self.voice.mark_command_ready()
                    await self._speak_with_barge_in("Go on.")
                    if followup < self.barge_in.maximum_followups:
                        await self.voice.begin_barge_in()
                        self.voice.vad.reset()
                        minimum_sequence = self.voice._sequence
                        continue
                    await self.voice.return_to_wake()
                    return

                if act == ConversationAct.CONTINUE and self._pending_response_chunks:
                    await self.voice.mark_command_ready()
                    barge_sequence = await self._continue_response()
                    if barge_sequence is not None and followup < self.barge_in.maximum_followups:
                        await self.voice.begin_barge_in()
                        self.voice.vad.reset()
                        minimum_sequence = barge_sequence
                        continue
                    await self.voice.return_to_wake()
                    return

                transcript_ready = monotonic()
                result = await asyncio.to_thread(self.runtime.command, text, "voice")
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
            await self.voice.fail_and_return_to_wake("ADVANCED_REALTIME_TURN_FAILED")
