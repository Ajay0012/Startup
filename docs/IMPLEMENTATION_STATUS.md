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
| Production wake model | EXTERNAL_DEPENDENCY_MISSING | expected real wake model/provider validation still required |
| Gesture perception | IMPLEMENTED_UNVALIDATED | `gestures.py`; MediaPipe Hand Landmarker + deterministic temporal gesture recognition |
| Spatial hand interaction proposals | IMPLEMENTED_UNVALIDATED | `spatial_interaction.py`; no direct OS execution from model/gesture output |
| Native session agent | PARTIAL | current .NET host is still a mutex/foundation rather than the complete supervisor described by historical handoff |
| Native overlay | PARTIAL_DEGRADED | current host remains degraded contract mode; full interactive HUD validation remains outstanding |

## Production voice continuation — 2026-08-09

`RuntimeBuilder` no longer wires `FakeTranscriptionProvider` or `FakeWakePhraseVerifier` into the production composition. It now constructs `FasterWhisperTranscriptionProvider` using the local `models/voice/whisper` directory and uses `TranscriptionWakePhraseVerifier` for fail-closed phrase confirmation. The provider never downloads a model implicitly. Missing model/backend states are returned as explicit unavailable results rather than fabricated transcripts.

The remaining P0 voice blocker is the wake-word implementation/artifact. The existing `SherpaOnnxWakeWordEngine` boundary must not be called production-complete until a real wake model and real repeated microphone wake-to-response validation exist.

## Gesture / spatial interaction — 2026-08-09

PANGU now contains an optional gesture perception runtime integrated through the existing `RuntimeBuilder`, `LifecycleKernel`, and shared `EventBus`. Camera startup is opt-in through `PANGU_GESTURES_ENABLED`; disabled-by-default behavior prevents silent camera activation.

The gesture subsystem supports deterministic recognition of point, pinch, grab, open palm, directional swipes, two-hand scale, and two-hand rotation. `MediaPipeHandTracker` uses the on-device MediaPipe Tasks Hand Landmarker boundary and requires an explicit local model at `models/vision/hand_landmarker.task` by default. Camera frames are not persisted by the PANGU adapter.

`SpatialInteractionController` translates recognized gestures into typed proposals such as pointer movement, selection, grab/release, navigation, scale, and rotation. It deliberately does not inject raw mouse/keyboard/Win32 actions. Target resolution and any consequential action must continue through PANGU safety/capability controls.

Current gesture status is IMPLEMENTED_UNVALIDATED: regression tests were added, but camera/model/hardware validation must be performed on the Windows target machine before production claims are made.

## Persistence continuation — 2026-08-05

Alembic head is `0002_persistent_exact_approval`. `0001` owns the base thirteen runtime tables and `0002` expands the existing `approvals` table; production DDL remains Alembic-only. `DatabaseService` remains the sole engine owner and `persistence.py` is an import-compatibility facade.

`repositories.py` provides domain records and session-owned SQLAlchemy repositories. `approvals.py` provides canonical SHA-256-bound persistent approvals. One-time consumption uses a conditional update and creates its consumption history in the same transaction; revocation and revocation history are likewise atomic. Canonicalization sorts mapping keys, sets, and permission scopes while preserving list order, and emits UTC timestamps.

Historical validation recorded 33 Python tests passing at this earlier milestone; it must not be treated as proof for the current branch after later changes.

## Current validation requirement

Run `scripts/test.ps1` on a Windows checkout of this branch. It executes compileall, Ruff check, Ruff format check, mypy, pytest, and `dotnet test Pangu.sln`. Real microphone, wake-word, Faster Whisper, camera/gesture, session-agent, and overlay validation remain separate manual gates.
