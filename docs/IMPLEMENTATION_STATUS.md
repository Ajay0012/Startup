# Implementation status

Repository truth remains authoritative. Status labels distinguish code implementation from automated, integration, and hardware validation.

| Area | Status | Evidence |
|---|---|---|
| Deterministic command and SQLite audit slice | VERIFIED_AUTOMATED_HISTORICAL | `runtime.py`, `persistence.py`, pytest |
| Bounded EventBus | VERIFIED_AUTOMATED_HISTORICAL | `events.py`; lifecycle integration tests exist |
| Lifecycle Kernel | VERIFIED_AUTOMATED_HISTORICAL | `lifecycle.py` |
| Capability catalog and scoped permissions | VERIFIED_AUTOMATED_HISTORICAL | `capabilities.py`, `permissions.py`, `tools.py` |
| Exact approval primitive | VERIFIED_AUTOMATED_HISTORICAL | `security.py`, `approvals.py`, `tools.py` |
| Safe filesystem create/write/recycle boundary | VERIFIED_AUTOMATED_HISTORICAL | `filesystem.py`, `tools.py` |
| Local API | IMPLEMENTED | `apps/backend/main.py`; current branch execution validation still required |
| Gemini provider | IMPLEMENTED | SDK-isolated provider, bounded retries/circuit breaker/budgets; live validation environment-dependent |
| Microphone capture and VAD | IMPLEMENTED | `voice.py`; real hardware revalidation required |
| Production Faster Whisper provider | IMPLEMENTED_UNVALIDATED | `voice_providers.py`; lazy local-only model loading and truthful unavailable states |
| Advanced Hey Pangu wake provider | IMPLEMENTED_UNVALIDATED | `wake_word.py`, `production_voice.py`, `scripts/install-wake-model.ps1`; Windows CI and microphone validation required |
| Gesture perception | IMPLEMENTED_UNVALIDATED | `gestures.py`; MediaPipe Hand Landmarker + deterministic temporal gesture recognition |
| Spatial hand interaction proposals | IMPLEMENTED_UNVALIDATED | `spatial_interaction.py`; no direct OS execution from model/gesture output |
| Native session agent | PARTIAL | current .NET host is still a mutex/foundation rather than the complete supervisor described by historical handoff |
| Native overlay | PARTIAL_DEGRADED | current host remains degraded contract mode; full interactive HUD validation remains outstanding |

## Advanced wake-word continuation — 2026-08-09

The historical `SherpaOnnxWakeWordEngine` in `voice.py` was verified to be only a fake compatibility boundary and is no longer selected by `RuntimeBuilder`. Production composition now uses `SherpaKeywordSpotterWakeWordEngine` from `wake_word.py` and the existing single voice lifecycle is hardened by `ProductionVoiceSessionRuntime`.

Wake detection is local and fail-closed. The provider requires an explicit sherpa-onnx KWS model bundle under `models/voice/wake/sherpa-kws`, rejects low-energy windows before inference, accepts only an explicit PANGU label allowlist, applies bounded cooldown, supports TTS suppression, clears stale ring-buffer audio after a confirmed wake, and emits `voice.wake.detected` metadata without persisting microphone audio.

Supported configured labels include `PANGU`, `HEY_PANGU`, `HAY_PANGU`, `HEY_PANGUU`, and `HEY_PANGOO` so accent/pronunciation variants can be encoded as distinct keyword paths while canonical behavior remains PANGU/HEY PANGU. These variants are not a claim of hardware accuracy; thresholds must be calibrated against positive and negative microphone samples on the target machine.

`scripts/install-wake-model.ps1` installs the selected low-latency sherpa-onnx KWS model only when the caller supplies a trusted SHA-256 for the source archive. It generates a local pronunciation extension for PANGU and a tuned keyword file instead of auto-trusting an unverified download. No wake model is silently downloaded during normal PANGU startup.

`ProductionVoiceSessionRuntime.start()` now defaults to capture enabled, starts the existing bounded normalization worker and a bounded wake watcher inside the same authoritative voice runtime, and performs deterministic worker cleanup on shutdown. This fixes the previous state where lifecycle startup called `voice.start()` but did not actually begin production microphone capture.

The remaining wake gate is validation, not another implementation placeholder: the real KWS artifacts must be installed, Windows CI must pass, and false-accept/false-reject behavior must be measured with real microphone samples including silence, fan noise, keyboard noise, music/TV speech, near-field/far-field speech, and PANGU TTS playback.

## Production voice continuation — 2026-08-09

`RuntimeBuilder` no longer wires `FakeTranscriptionProvider` or `FakeWakePhraseVerifier` into the production composition. It constructs `FasterWhisperTranscriptionProvider` using the local `models/voice/whisper` directory. Wake confirmation no longer performs pre-wake Whisper transcription; the local KWS detector is the wake authority and `WakePhrasePolicyVerifier` only validates the already-detected phrase label.

The provider never downloads a Whisper model implicitly. Missing model/backend states are returned as explicit unavailable results rather than fabricated transcripts.

## Gesture / spatial interaction — 2026-08-09

PANGU contains an optional gesture perception runtime integrated through the existing `RuntimeBuilder`, `LifecycleKernel`, and shared `EventBus`. Camera startup is opt-in through `PANGU_GESTURES_ENABLED`; disabled-by-default behavior prevents silent camera activation.

The gesture subsystem supports deterministic recognition of point, pinch, grab, open palm, directional swipes, two-hand scale, and two-hand rotation. `MediaPipeHandTracker` uses the on-device MediaPipe Tasks Hand Landmarker boundary and requires an explicit local model at `models/vision/hand_landmarker.task` by default. Camera frames are not persisted by the PANGU adapter.

`SpatialInteractionController` translates recognized gestures into typed proposals such as pointer movement, selection, grab/release, navigation, scale, and rotation. It deliberately does not inject raw mouse/keyboard/Win32 actions. Target resolution and any consequential action must continue through PANGU safety/capability controls.

Current gesture status is IMPLEMENTED_UNVALIDATED: regression tests were added, but camera/model/hardware validation must be performed on the Windows target machine before production claims are made.

## Persistence continuation — 2026-08-05

Alembic head is `0002_persistent_exact_approval`. `0001` owns the base thirteen runtime tables and `0002` expands the existing `approvals` table; production DDL remains Alembic-only. `DatabaseService` remains the sole engine owner and `persistence.py` is an import-compatibility facade.

`repositories.py` provides domain records and session-owned SQLAlchemy repositories. `approvals.py` provides canonical SHA-256-bound persistent approvals. One-time consumption uses a conditional update and creates its consumption history in the same transaction; revocation and revocation history are likewise atomic. Canonicalization sorts mapping keys, sets, and permission scopes while preserving list order, and emits UTC timestamps.

Historical validation recorded 33 Python tests passing at this earlier milestone; it must not be treated as proof for the current branch after later changes.

## Current validation requirement

`.github/workflows/ci.yml` runs the full Windows validation gate for pushes and pull requests: compileall, Ruff check, Ruff format check, mypy, pytest, and `dotnet test Pangu.sln` through `scripts/test.ps1`.

Hardware validation remains separate for microphone/wake-word accuracy, Faster Whisper transcription, camera/gesture interaction, session-agent login startup, and the native overlay.
