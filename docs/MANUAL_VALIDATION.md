# Manual validation

1. Run `scripts/bootstrap.ps1`, then `scripts/test.ps1`.
2. Run `scripts/development.ps1`; call `GET http://127.0.0.1:8765/health` and confirm no public binding.
3. With a test workspace, run `pangu "create folder reports"` and verify the folder and SQLite audit record.
4. Install/prepare the Windows overlay workload and validate click-through, DPI, multi-monitor behavior, interaction mode, and lifecycle manually.
5. Add a valid Gemini key locally and run a health check; never copy the key to a report.
6. Connect a microphone and validate capture, wake-word, VAD, Faster Whisper, TTS echo suppression, barge-in, and return-to-wake using real local model artifacts.

## Database lifecycle validation

Run `GET /health` to inspect only sanitized database state. `GET /ready` returns 200 only after the lifecycle startup has migrated and admitted database work; it returns 503 otherwise. Run `python -m pytest -q -p no:cacheprovider` with a writable temporary directory.

## Gemini manual validation

With a valid local `GEMINI_API_KEY`, run `pangu model-health`, then `pangu route "research a topic"`. Confirm that health output never contains a key or prompt. Do not treat an unconfigured provider as a failed deterministic runtime.

## Advanced Hey Pangu wake-word validation

The production wake path uses the local sherpa-onnx open-vocabulary KWS provider in `wake_word.py`. It does not use Whisper to detect the wake phrase and it does not send microphone audio to Gemini.

### Install the wake model

1. Obtain the trusted SHA-256 for the exact sherpa-onnx KWS release archive you intend to use.
2. Set `PANGU_WAKE_ARCHIVE_SHA256` locally. Do not commit it merely as a placeholder.
3. Run `scripts/install-wake-model.ps1`.
4. Confirm these files exist under `models/voice/wake/sherpa-kws`: `encoder.onnx`, `decoder.onnx`, `joiner.onnx`, `tokens.txt`, `keywords.txt`, `en.phone`, and `manifest.json`.
5. Confirm the generated manifest contains hashes for installed artifacts.
6. Start PANGU and confirm missing/corrupt artifacts report an unavailable/degraded wake state rather than a successful wake.

### Positive wake tests

Record results across multiple distances, speaking rates, and volumes. Test at minimum:

- `Pangu`
- `Hey Pangu`
- `Hay Pangu`
- a slightly lengthened final vowel such as `Hey Panguu`
- near-field speech at approximately arm's length
- far-field speech from across the room
- normal, quiet, and moderately loud speech
- different room noise levels

Run at least 20 trials for each primary phrase (`Pangu` and `Hey Pangu`) before accepting the threshold configuration. Record successful detections, misses, and latency instead of reporting only subjective success.

### Negative/false-trigger tests

Leave PANGU listening while exposing the microphone to:

- silence
- fan/AC noise
- keyboard typing
- mouse clicks
- music
- television/video speech
- normal conversation that does not contain PANGU
- words with similar sounds such as `panda`, `bangle`, `thank you`, and `can you`
- PANGU's own TTS output

Confirm no expensive command transcription begins before a valid wake. Confirm the stale ring buffer is cleared after wake confirmation, cooldown blocks immediate duplicate triggers, and TTS suppression prevents self-retriggering.

### Threshold tuning

Tune keyword boosting and trigger thresholds in `keywords_raw.txt` only from measured false-accept/false-reject evidence. Do not simply lower thresholds until the positive examples work: every sensitivity increase must be retested against the negative corpus.

A wake configuration is not production-validated until it passes repeated positive/negative trials on the target microphone and room conditions.

## Faster Whisper validation

1. Place a compatible local Faster Whisper model directory at `models/voice/whisper`.
2. Do not use a network model identifier for production startup; PANGU intentionally requests local files only.
3. Run the focused voice provider tests and the full test suite.
4. Exercise real microphone speech in English, Tamil, and Tanglish.
5. Verify unavailable behavior by temporarily moving the model directory and confirming `WHISPER_MODEL_UNAVAILABLE` rather than a fabricated transcript.
6. Measure transcription latency and confirm no raw microphone audio is persisted.

## Hand gesture / spatial interaction validation

Prerequisites:

- Install the optional vision dependencies from the project (`mediapipe` and `opencv-python`).
- Place a compatible MediaPipe Hand Landmarker model at `models/vision/hand_landmarker.task` or set `PANGU_GESTURE_MODEL_PATH` to another local repository-relative path.
- Connect the intended camera.

Validation sequence:

1. Leave `PANGU_GESTURES_ENABLED=false`; start PANGU and confirm the camera is not opened.
2. Set `PANGU_GESTURES_ENABLED=true` and choose `PANGU_GESTURE_CAMERA_INDEX` if the default camera is not correct.
3. Start PANGU and verify gesture diagnostics report `READY` only when the model, MediaPipe/OpenCV backend, and camera are actually available.
4. Validate one-hand point, pinch, grab, open-palm release, and four directional swipes.
5. Validate two-hand scale-in/scale-out and clockwise/counter-clockwise rotation.
6. Confirm `gesture.detected` events contain only landmark-derived metadata and do not persist raw camera frames.
7. Confirm `SpatialInteractionController` produces typed proposals rather than direct OS input.
8. Integrate proposals with the HUD target resolver and safety/capability boundary before enabling any real click/drag/window action.
9. Test under different lighting, hand sizes, backgrounds, camera positions, and both left/right handedness.
10. Perform a sustained camera session and verify clean release of camera and MediaPipe resources on shutdown/restart.

Do not call the gesture feature production-validated until these Windows hardware checks have passed repeatedly.
