import asyncio

import pytest

from pangu.events import EventBus
from pangu.language import LanguageRuntime
from pangu.voice import (
    AudioDevice,
    DeviceDisconnectedError,
    FakeAudioInputAdapter,
    FakeTranscriptionProvider,
    FakeVad,
    FakeWakePhraseVerifier,
    FakeWakeWordEngine,
    VoiceCaptureRequest,
    VoiceConfig,
    VoiceSessionRuntime,
    VoiceState,
)


def test_fake_voice_runtime_start_stop() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        voice = VoiceSessionRuntime(
            FakeAudioInputAdapter(),
            FakeVad(),
            FakeWakeWordEngine(),
            FakeWakePhraseVerifier(),
            FakeTranscriptionProvider(),
            events,
            LanguageRuntime(),
        )
        await voice.start()
        assert voice.state == VoiceState.IDLE_LISTENING
        await voice.stop()
        assert voice.state == VoiceState.STOPPED
        await events.stop()

    asyncio.run(run())


def test_missing_microphone_fails_closed() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        voice = VoiceSessionRuntime(
            FakeAudioInputAdapter([]),
            FakeVad(),
            FakeWakeWordEngine(),
            FakeWakePhraseVerifier(),
            FakeTranscriptionProvider(),
            events,
            LanguageRuntime(),
        )
        await voice.start()
        assert voice.state == VoiceState.DEVICE_UNAVAILABLE
        await voice.stop()
        await events.stop()

    asyncio.run(run())


def test_illegal_transition_is_rejected() -> None:
    voice = VoiceSessionRuntime(
        FakeAudioInputAdapter([AudioDevice("default", "mic", True, 1)]),
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        EventBus(),
        LanguageRuntime(),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(voice._transition(VoiceState.COMMAND_LISTENING, "invalid"))


class FramesAdapter(FakeAudioInputAdapter):
    def start(self, selector, on_frame=None):
        if on_frame:
            for _ in range(4):
                on_frame((0.1,) * 16000, 1, 16000, False)


class DisconnectingAdapter(FakeAudioInputAdapter):
    def start(self, selector, on_frame=None):
        raise DeviceDisconnectedError()


def build_voice(adapter, events):
    return VoiceSessionRuntime(
        adapter,
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        events,
        LanguageRuntime(),
    )


def test_capture_uses_fake_frames_and_cleans_up() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        voice = VoiceSessionRuntime(
            FramesAdapter(),
            FakeVad(),
            FakeWakeWordEngine(),
            FakeWakePhraseVerifier(),
            FakeTranscriptionProvider(),
            events,
            LanguageRuntime(),
        )
        result = await voice.capture_test(VoiceCaptureRequest(1))
        assert result.verification_state == "VERIFIED"
        assert result.worker_stopped and result.cleanup_verified
        assert result.total_samples > 0 and result.rms_level is not None
        await events.stop()

    asyncio.run(run())


def test_capture_cancelled_is_unverified() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        voice = VoiceSessionRuntime(
            FakeAudioInputAdapter(),
            FakeVad(),
            FakeWakeWordEngine(),
            FakeWakePhraseVerifier(),
            FakeTranscriptionProvider(),
            events,
            LanguageRuntime(),
        )
        task = asyncio.create_task(voice.capture_test(VoiceCaptureRequest(2)))
        await asyncio.sleep(0)
        task.cancel()
        result = await task
        assert result.normalized_error == "CANCELLED"
        assert result.verification_state == "UNVERIFIED"
        await events.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "selector,expected", [(None, "VERIFIED"), ("default", "VERIFIED"), ("stale", "UNVERIFIED")]
)
def test_device_selection(selector, expected) -> None:
    voice = VoiceSessionRuntime(
        FakeAudioInputAdapter(),
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        EventBus(),
        LanguageRuntime(),
    )
    assert voice.select_device(selector).verification_state == expected


def test_ambiguous_devices_fail_closed() -> None:
    voice = VoiceSessionRuntime(
        FakeAudioInputAdapter(
            [AudioDevice("a", "same", False, 1), AudioDevice("b", "same", False, 1)]
        ),
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        EventBus(),
        LanguageRuntime(),
    )
    assert voice.select_device().normalized_error == "AMBIGUOUS_DEVICE"


def test_callback_ignored_after_stop() -> None:
    voice = VoiceSessionRuntime(
        FakeAudioInputAdapter(),
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        EventBus(),
        LanguageRuntime(),
    )
    voice.accept_callback_frame((0.1,), 1, 16000)
    assert voice.metrics["frames_received"] == 0


def test_resampler_downmixes_stereo() -> None:
    voice = VoiceSessionRuntime(
        FakeAudioInputAdapter(),
        FakeVad(),
        FakeWakeWordEngine(),
        FakeWakePhraseVerifier(),
        FakeTranscriptionProvider(),
        EventBus(),
        LanguageRuntime(),
        VoiceConfig(),
    )
    frame = voice.resampler.normalize((1.0, -1.0, 0.5, 0.5), 2, 16000, 1, 1)
    assert frame and frame.samples == (0.0, 0.5)


def test_device_disconnection_returns_typed_failure() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        result = await build_voice(DisconnectingAdapter(), events).capture_test(
            VoiceCaptureRequest(1)
        )
        assert result.normalized_error == "DEVICE_DISCONNECTED" and result.retryable
        await events.stop()

    asyncio.run(run())


def test_device_disconnection_cleans_worker_and_buffer() -> None:
    async def run() -> None:
        events = EventBus()
        await events.start()
        voice = build_voice(DisconnectingAdapter(), events)
        result = await voice.capture_test(VoiceCaptureRequest(1))
        assert result.worker_stopped and result.ring_buffer_cleared
        await events.stop()

    asyncio.run(run())


def test_capture_started_and_completed_events_are_emitted() -> None:
    async def run() -> None:
        events = EventBus()
        seen = []

        async def collect(event):
            seen.append(event)

        events.subscribe("voice.capture.started", collect)
        events.subscribe("voice.capture.completed", collect)
        await events.start()
        await build_voice(FramesAdapter(), events).capture_test(VoiceCaptureRequest(1))
        await asyncio.sleep(0.01)
        assert {event.event_type for event in seen} == {
            "voice.capture.started",
            "voice.capture.completed",
        }
        await events.stop()

    asyncio.run(run())


def test_capture_cancelled_event_is_emitted() -> None:
    async def run() -> None:
        events = EventBus()
        seen = []

        async def collect(event):
            seen.append(event.event_type)

        events.subscribe("voice.capture.cancelled", collect)
        await events.start()
        voice = build_voice(FakeAudioInputAdapter(), events)
        task = asyncio.create_task(voice.capture_test(VoiceCaptureRequest(2)))
        await asyncio.sleep(0)
        task.cancel()
        await task
        await asyncio.sleep(0.01)
        assert "voice.capture.cancelled" in seen
        await events.stop()

    asyncio.run(run())


def test_capture_event_payload_contains_no_raw_audio() -> None:
    async def run() -> None:
        events = EventBus()
        payloads = []

        async def collect(event):
            payloads.append(event.payload)

        events.subscribe("voice.capture.completed", collect)
        await events.start()
        await build_voice(FramesAdapter(), events).capture_test(VoiceCaptureRequest(1))
        await asyncio.sleep(0.01)
        assert all(
            not {"samples", "raw_audio", "stream", "thread"} & set(payload) for payload in payloads
        )
        await events.stop()

    asyncio.run(run())
