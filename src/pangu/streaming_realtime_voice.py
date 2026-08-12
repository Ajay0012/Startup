from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
from time import monotonic

from .advanced_realtime_voice import AdvancedRealtimeVoiceTurnCoordinator
from .conversation_intelligence import ConversationAct
from .events import EventEnvelope, EventPriority
from .realtime_voice import RealtimeTurnMetrics, RealtimeVoiceTurnCoordinator
from .voice import AudioFrame, VoiceState


class StreamingAdvancedRealtimeVoiceTurnCoordinator(AdvancedRealtimeVoiceTurnCoordinator):
    """Speak true Gemini stream chunks without creating a second voice owner."""

    @staticmethod
    def _sentence_ready(buffer: str) -> bool:
        words = buffer.split()
        return len(words) >= 10 and (
            buffer.rstrip().endswith((".", "?", "!", ";", ":")) or len(words) >= 24
        )

    async def _streaming_source(self, text: str) -> AsyncIterator[str]:
        method = getattr(self.runtime, "stream_command", None)
        if not callable(method):
            result = await asyncio.to_thread(self.runtime.command, text, "voice")
            yield result.message
            return
        stream = method(text, "voice")
        async for chunk in stream:
            yield str(chunk)

    async def _speak_streaming_response(self, text: str) -> tuple[int | None, float, str]:
        started = monotonic()
        first_chunk_ms = 0.0
        first_seen = False
        buffer = ""
        complete = ""
        stream = self._streaming_source(text)
        try:
            async for delta in stream:
                if not delta:
                    continue
                if not first_seen:
                    first_seen = True
                    first_chunk_ms = (monotonic() - started) * 1000
                    await self.events.publish(
                        EventEnvelope(
                            "voice.response.first_model_chunk",
                            {
                                "session_id": self.voice.session_id,
                                "latency_ms": first_chunk_ms,
                            },
                            EventPriority.LOW,
                        )
                    )
                complete += delta
                buffer += delta
                if not self._sentence_ready(buffer):
                    continue
                piece = buffer.strip()
                buffer = ""
                await self.events.publish(
                    EventEnvelope(
                        "voice.response.stream_chunk",
                        {"session_id": self.voice.session_id, "text": piece},
                        EventPriority.LOW,
                    )
                )
                barge = await RealtimeVoiceTurnCoordinator._speak_with_barge_in(self, piece)
                if barge is not None:
                    return barge, first_chunk_ms, complete
            if buffer.strip():
                piece = buffer.strip()
                barge = await RealtimeVoiceTurnCoordinator._speak_with_barge_in(self, piece)
                if barge is not None:
                    return barge, first_chunk_ms, complete
        finally:
            close = getattr(stream, "aclose", None)
            if callable(close):
                await close()
        self._last_response_text = complete.strip() or None
        return None, first_chunk_ms, complete

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
                    barge = await self._speak_with_barge_in("I didn't catch that.")
                    if barge is not None and followup < self.barge_in.maximum_followups:
                        await self.voice.begin_barge_in()
                        self.voice.vad.reset()
                        minimum_sequence = barge
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
                    barge = await self._speak_with_barge_in("I couldn't understand that clearly.")
                    if barge is not None and followup < self.barge_in.maximum_followups:
                        await self.voice.begin_barge_in()
                        self.voice.vad.reset()
                        minimum_sequence = barge
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
                    barge = await self._continue_response()
                    if barge is not None and followup < self.barge_in.maximum_followups:
                        await self.voice.begin_barge_in()
                        self.voice.vad.reset()
                        minimum_sequence = barge
                        continue
                    await self.voice.return_to_wake()
                    return

                intent = self.runtime.language.normalize(text)
                transcript_ready = monotonic()
                if intent.intent_name == "informational":
                    await self.voice.mark_command_ready()
                    barge, first_chunk_ms, _complete = await self._speak_streaming_response(text)
                    transcript_to_result_ms = first_chunk_ms
                    result_to_speech_ms = first_chunk_ms
                else:
                    result = await asyncio.to_thread(self.runtime.command, text, "voice")
                    transcript_to_result_ms = (monotonic() - transcript_ready) * 1000
                    await self.voice.mark_command_ready()
                    result_ready = monotonic()
                    barge = await self._speak_with_barge_in(result.message)
                    result_to_speech_ms = (monotonic() - result_ready) * 1000

                if barge is not None and followup < self.barge_in.maximum_followups:
                    await self.voice.begin_barge_in()
                    self.voice.vad.reset()
                    minimum_sequence = barge
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
            await self.voice.fail_and_return_to_wake("STREAMING_REALTIME_TURN_FAILED")
