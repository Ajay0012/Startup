import asyncio

import pytest

from pangu.events import EventBus, EventEnvelope
from pangu.production_voice import ProductionVoiceSessionRuntime, WakePhrasePolicyVerifier
from pangu.realtime_voice import BargeInPolicy
from pangu.tts import NullSpeechProvider
from pangu.voice import (
    FakeAudioInputAdapter,
    FakeTranscriptionProvider,
    FakeVad,
    FakeWakeWordEngine,
    LanguageRuntime,
    VoiceState,
    WakeDetection,
)


def detection(keyword: str = "hey pangu") -> WakeDetection:
    return WakeDetection(keyword, keyword, 0.0, 0.5, 0.9, 0.5, "test-kws", "session")


@pytest.mark.asyncio
async def test_confirmed_wake_enters_command_listening_and_clears_buffer() -> None:
    events = EventBus()
    await events.start()
    voice = ProductionVoiceSessionRuntime(
        FakeAudioInputAdapter(),
        FakeVad(),
        FakeWakeWordEngine(),
        WakePhrasePolicyVerifier(),
        FakeTranscriptionProvider(),
        events,
        LanguageRuntime(),
    )
    voice.state = VoiceState.IDLE_LISTENING
    voice.session_id = "session"
    await voice._handle_wake(detection())
    assert voice.state == VoiceState.COMMAND_LISTENING
    assert voice.frames.duration_ms() == 0
    await events.stop()


@pytest.mark.asyncio
async def test_turn_state_returns_to_wake_listening() -> None:
    events = EventBus()
    await events.start()
    voice = ProductionVoiceSessionRuntime(
        FakeAudioInputAdapter(),
        FakeVad(),
        FakeWakeWordEngine(),
        WakePhrasePolicyVerifier(),
        FakeTranscriptionProvider(),
        events,
        LanguageRuntime(),
    )
    voice.state = VoiceState.COMMAND_LISTENING
    voice.session_id = "session"
    await voice.begin_transcription()
    assert voice.state == VoiceState.TRANSCRIBING
    await voice.mark_command_ready()
    assert voice.state == VoiceState.COMMAND_READY
    await voice.return_to_wake()
    assert voice.state == VoiceState.IDLE_LISTENING
    await events.stop()


@pytest.mark.asyncio
async def test_barge_in_reuses_same_voice_runtime_without_new_wake() -> None:
    events = EventBus()
    await events.start()
    voice = ProductionVoiceSessionRuntime(
        FakeAudioInputAdapter(),
        FakeVad(),
        FakeWakeWordEngine(),
        WakePhrasePolicyVerifier(),
        FakeTranscriptionProvider(),
        events,
        LanguageRuntime(),
    )
    voice.state = VoiceState.COMMAND_READY
    voice.session_id = "session"
    await voice.begin_barge_in()
    assert voice.state == VoiceState.COMMAND_LISTENING
    assert voice.metrics["barge_ins"] == 1
    await events.stop()


def test_barge_in_policy_is_bounded() -> None:
    policy = BargeInPolicy()
    assert policy.maximum_followups == 3
    with pytest.raises(ValueError):
        BargeInPolicy(maximum_followups=100)


@pytest.mark.asyncio
async def test_event_unsubscribe_prevents_duplicate_lifecycle_handlers() -> None:
    events = EventBus()
    received = 0

    async def handler(_: EventEnvelope) -> None:
        nonlocal received
        received += 1

    events.subscribe("turn", handler)
    events.unsubscribe("turn", handler)
    await events.start()
    await events.publish(EventEnvelope("turn", {}))
    await asyncio.sleep(0.02)
    await events.stop()
    assert received == 0


@pytest.mark.asyncio
async def test_null_speech_provider_is_explicitly_disabled() -> None:
    result = await NullSpeechProvider().speak("hello")
    assert result.provider == "none"
    assert result.verification_state == "DISABLED"
