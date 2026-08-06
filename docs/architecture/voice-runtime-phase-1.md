# Voice runtime Phase 1

The input pipeline is local and bounded: microphone discovery, normalized frames, VAD, KWS, phrase confirmation, turn capture, local transcription, then existing language normalization. `RuntimeBuilder` owns the runtime and lifecycle; no audio is written to disk or uploaded.

## Phase 1A

Phase 1A provides Windows device discovery through sounddevice/PortAudio, sanitized session-local selectors, optional bounded capture, mono float32 16 kHz normalization, a fixed-capacity non-blocking queue, and a bounded in-memory ring buffer. Lifecycle startup only discovers devices; it never opens a microphone. Diagnostics and the read-only API expose device and operational metrics but never samples. Phase 1B will add KWS/VAD; Phase 1C will add local transcription and routing.

Capture uses a callback that only enqueues bounded raw frames; a concurrent worker normalizes, resamples, aggregates levels, and clears all in-memory audio at completion. Newest frames are dropped when the bounded queue is full and this is reflected in the result. Capture is verified only after stream closure, worker termination, queue accounting, and ring-buffer cleanup. Cancellation returns `CANCELLED`; adapter-reported loss returns `DEVICE_DISCONNECTED` without automatic microphone switching. Events contain only selectors, counts, durations, verification state, and normalized errors—never samples or native objects. `voice capture-test` exits 0 for verified capture, 6 for device loss/native failure, 7 for unsupported backend, and 8 for cancelled or unverified capture.

Default wake phrase is **Hey Pangu**. Production uses sherpa-onnx KWS with Silero VAD and local Faster Whisper (`cpu-balanced`: small/cpu/int8). Models are explicitly installed under `models/voice`; startup never downloads models. The legacy `pangu.onnx` boundary is disabled and is not selected.

The state machine is `STOPPED → INITIALIZING → IDLE_LISTENING → SPEECH_CANDIDATE → WAKE_CANDIDATE → WAKE_CONFIRMED → COMMAND_LISTENING → TURN_ENDING → TRANSCRIBING → COMMAND_READY → COOLDOWN`. Illegal transitions fail closed. Future response/barge-in and consented friend-voice synthesis are contracts only: no TTS, cloning, or audio output is implemented.

Friend voices require recorded informed consent, local restricted storage, revocation/removal, traceable providers, and a non-cloned fallback. No custom model may load without a valid local consent record.
# Phase 1B1 deterministic core

Phase 1B1 adds an input-only, fake-testable VAD segmentation core.  It does not
load Silero/sherpa models, open a microphone, persist calibration/audio, invoke
wake words, Whisper, Gemini, command routing, or TTS.

`VoiceActivityResult` is a scalar contract: finite timestamp, probability in
`[0, 1]`, non-negative energy, a positive finite frame duration, and a boolean
speech decision. A frame is speech only when that contract and samples are
valid, `is_speech`, `probability >= speech_threshold`, and the VAD energy gate
passes. Energy supplements VAD; it never replaces it.

The controller transitions `IDLE -> CANDIDATE -> SPEAKING -> ENDING -> IDLE`.
A candidate must accumulate `minimum_speech_ms`; a short candidate is rejected.
During IDLE a chronological bounded prefix is retained. It is prepended exactly
once only after acceptance. ENDING retains only bounded trailing padding; resumed
speech before `minimum_silence_ms` returns to SPEAKING, while sustained silence
stops the segment. Cancellation, device loss, shutdown, VAD failure and maximum
duration safely clear all transient state.

The retained-sample upper bound is `sample_rate * maximum_utterance_seconds +
sample_rate * (prefix_padding_ms + trailing_padding_ms) / 1000`; active utterance
audio itself is capped at the first term. `SpeechSegment` keeps private,
repr-disabled samples solely for its immediate in-memory consumer. `public()` and
all emitted metadata exclude samples. `clear_samples()` drops audio while leaving
metadata usable.

Ambient calibration uses a bounded RMS reservoir (256 values by default), rather
than raw audio or an unbounded percentile list. It reports duration, sample and
percentile evidence, contamination, confidence and normalized failure details.
Only a VERIFIED profile can raise the active gate; an unverified profile can
never lower it below `minimum_energy_floor`. Profiles are not persisted.

Metadata-only core events are calibration started/completed/failed, speech
candidate/started/stopped/rejected, and VAD error. Subscriber failures are
isolated by the event bus and do not retain audio. Tests use fake frames and fake
detectors only. Phase 1B2 will add explicit real Silero integration; Phase 1B3
will validate live microphone operation separately.
