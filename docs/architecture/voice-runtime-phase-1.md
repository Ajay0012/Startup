# Voice runtime Phase 1

The input pipeline is local and bounded: microphone discovery, normalized frames, VAD, KWS, phrase confirmation, turn capture, local transcription, then existing language normalization. `RuntimeBuilder` owns the runtime and lifecycle; no audio is written to disk or uploaded.

## Phase 1A

Phase 1A provides Windows device discovery through sounddevice/PortAudio, sanitized session-local selectors, optional bounded capture, mono float32 16 kHz normalization, a fixed-capacity non-blocking queue, and a bounded in-memory ring buffer. Lifecycle startup only discovers devices; it never opens a microphone. Diagnostics and the read-only API expose device and operational metrics but never samples. Phase 1B will add KWS/VAD; Phase 1C will add local transcription and routing.

Capture uses a callback that only enqueues bounded raw frames; a concurrent worker normalizes, resamples, aggregates levels, and clears all in-memory audio at completion. Newest frames are dropped when the bounded queue is full and this is reflected in the result. Capture is verified only after stream closure, worker termination, queue accounting, and ring-buffer cleanup. Cancellation returns `CANCELLED`; adapter-reported loss returns `DEVICE_DISCONNECTED` without automatic microphone switching. Events contain only selectors, counts, durations, verification state, and normalized errors—never samples or native objects. `voice capture-test` exits 0 for verified capture, 6 for device loss/native failure, 7 for unsupported backend, and 8 for cancelled or unverified capture.

Default wake phrase is **Hey Pangu**. Production uses sherpa-onnx KWS with Silero VAD and local Faster Whisper (`cpu-balanced`: small/cpu/int8). Models are explicitly installed under `models/voice`; startup never downloads models. The legacy `pangu.onnx` boundary is disabled and is not selected.

The state machine is `STOPPED → INITIALIZING → IDLE_LISTENING → SPEECH_CANDIDATE → WAKE_CANDIDATE → WAKE_CONFIRMED → COMMAND_LISTENING → TURN_ENDING → TRANSCRIBING → COMMAND_READY → COOLDOWN`. Illegal transitions fail closed. Future response/barge-in and consented friend-voice synthesis are contracts only: no TTS, cloning, or audio output is implemented.

Friend voices require recorded informed consent, local restricted storage, revocation/removal, traceable providers, and a non-cloned fallback. No custom model may load without a valid local consent record.
