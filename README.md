# PANGU AI

PANGU AI is a local-first, safety-controlled Windows operating layer. The repository contains the deterministic command pipeline, SQLite audit/memory foundation, exact approvals, loopback API boundary, Windows adapters, native-process foundations, local voice processing, and an opt-in hand-gesture/spatial-interaction subsystem.

## Quick start

```powershell
./scripts/bootstrap.ps1
./scripts/development.ps1
py -3.12 -m pangu.cli "create folder reports"
```

The runtime remains useful without Gemini. Add `GEMINI_API_KEY` to the local `.env` to enable the isolated provider when its optional package is installed.

Gemini requests are isolated behind an injectable transport with bounded retries, circuit breaking, mission budgets, sanitization, and typed structured-output validation. `pangu models`, `pangu model-health`, and `pangu route "<text>"` expose only sanitized provider state.

`pangu model-health` is read-only and never contacts Gemini. To make one optional, context-free live health request using the configured fast model and timeout, run:

```powershell
pangu model-health --probe
```

## Local voice

Production composition uses local Faster Whisper plus the advanced sherpa-onnx keyword spotter for `Pangu` / `Hey Pangu`. Wake inference is local and happens before command transcription. The wake model is an explicit external artifact and is never silently downloaded at runtime.

Install a verified wake model bundle with:

```powershell
$env:PANGU_WAKE_ARCHIVE_SHA256 = '<trusted 64-character sha256>'
./scripts/install-wake-model.ps1
```

Then run the documented microphone/noise validation in `docs/MANUAL_VALIDATION.md`. Do not treat model installation alone as proof of wake-word accuracy.

## Hand gestures

Gesture capture is disabled by default. When the MediaPipe Hand Landmarker model is installed locally, enable it with:

```dotenv
PANGU_GESTURES_ENABLED=true
PANGU_GESTURE_MODEL_PATH=models/vision/hand_landmarker.task
```

The gesture runtime produces typed spatial interaction proposals; camera/model output never directly becomes an operating-system action.
