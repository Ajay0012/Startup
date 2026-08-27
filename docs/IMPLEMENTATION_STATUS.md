# PANGU implementation status

Repository truth is authoritative. Status labels deliberately separate **implemented code**, **Windows CI validation**, and **real target-machine / hardware validation**. Hardware-dependent functionality is never promoted to VERIFIED_HARDWARE from CI alone.

## Core architecture

| Area | Status | Evidence / boundary |
|---|---|---|
| Single RuntimeBuilder composition root | VERIFIED_CI_CHECKPOINT | `runtime_builder.py`; one DB, EventBus, voice lifecycle and session agent composition |
| SQLite persistence and audit | VERIFIED_CI_CHECKPOINT | `database.py`, repositories, Alembic migrations |
| Bounded EventBus and LifecycleKernel | VERIFIED_CI_CHECKPOINT | `events.py`, `lifecycle.py` |
| Capabilities and scoped permissions | VERIFIED_CI_CHECKPOINT | `capabilities.py`, `permissions.py` |
| Persistent exact approvals | IMPLEMENTED_MIGRATION | `approvals.py` supports actor/tool/version/op/args/target/risk/scopes/mission/session/expiry binding. Generic `Runtime` constructor still retains a compatibility `ApprovalStore` object and must be fully removed before declaring one-authority migration complete. |
| Resilient load manager | VERIFIED_CI_CHECKPOINT | `resilience.py`: bounded queue/concurrency, weighted routing, retry/deadline, load shedding, circuit open/half-open, EWMA latency |
| Active self-healing monitor | VERIFIED_CI_CHECKPOINT | `resilience_runtime.py`, lifecycle composition |

## Natural conversation / voice

| Area | Status | Evidence / boundary |
|---|---|---|
| Local wake word | IMPLEMENTED_NEEDS_HARDWARE | sherpa-onnx KWS, `wake_word.py`, explicit local models, TTS suppression and cooldown |
| Microphone + VAD | IMPLEMENTED_NEEDS_HARDWARE | single `ProductionVoiceSessionRuntime`, Silero VAD boundary |
| Faster Whisper STT | IMPLEMENTED_NEEDS_HARDWARE | local-only `voice_providers.py`; no silent model download |
| Rolling partial transcription | IMPLEMENTED_NEEDS_CURRENT_CI | `advanced_realtime_voice.py`; bounded rolling Faster Whisper hypotheses |
| Semantic end-of-turn | IMPLEMENTED_NEEDS_CURRENT_CI | linguistic + silence decision via `SemanticEndOfTurnDetector` |
| Barge-in/interruption recovery | IMPLEMENTED_NEEDS_HARDWARE | existing real-time coordinator plus pending-response chunk continuation |
| Backchannel/continue/cancel/repair routing | IMPLEMENTED_NEEDS_CURRENT_CI | final transcript classified before command execution |
| Chunked low-latency speech output | IMPLEMENTED_NEEDS_HARDWARE | response chunking + Windows SAPI interruption |
| Native token-streaming ASR/LLM/TTS | PARTIAL | Faster Whisper path is rolling re-transcription, not a native token stream; Google/model and TTS streaming adapters remain future provider work |
| Acoustic echo control | PARTIAL_NEEDS_HARDWARE | wake/TTS suppression + adaptive energy gating exist; production AEC requires target audio-stack validation/integration |

## Screen / visual computer use

| Area | Status | Evidence / boundary |
|---|---|---|
| UI Automation perception | VERIFIED_CI_CHECKPOINT | `screen_perception.py` |
| Privacy-controlled semantic screen observer | IMPLEMENTED_NEEDS_CURRENT_CI | `screen_observer.py`; opt-in, bounded semantic history, dedupe, password-context suppression, no pixel persistence |
| Local OCR | IMPLEMENTED_NEEDS_HARDWARE | `ocr_tesseract.py`; image bytes piped through stdin, no screenshot files |
| Element tracking across frames | VERIFIED_CI_CHECKPOINT | `screen_vision.py` temporal target tracker |
| Accessibility-first visual fallback | VERIFIED_CI_CHECKPOINT | `visual_computer_use.py`, `computer_use.py`; fresh screenshot → unambiguous visual target → deterministic coordinate → guarded action → fresh verification |
| Icon/image/object detection | IMPLEMENTED_NEEDS_HARDWARE | local OpenCV DNN YOLO-style ONNX provider |
| Chart/graph semantic comparison | PARTIAL | OCR/object primitives and multimodal context exist; dedicated chart-data extraction/comparison models are not hardware/integration validated |
| Raw model-generated coordinates | PROHIBITED | visual actions derive coordinates from freshly observed target bounds; model output does not inject arbitrary coordinates |

## Multimodal / spatial intelligence

| Area | Status | Evidence / boundary |
|---|---|---|
| Shared multimodal context | VERIFIED_CI_CHECKPOINT | `multimodal.py`: voice/screen/camera/gesture/browser/windows/memory/file/mission signals |
| Conversational reference resolution | VERIFIED_CI_CHECKPOINT | `contextual_nlu.py`, `advanced_language.py`; recent target/context resolution |
| Point + voice referent fusion | VERIFIED_CI_CHECKPOINT | `spatial_fusion.py`, gesture target IDs enter shared context |
| Gesture recognition | IMPLEMENTED_NEEDS_HARDWARE | point/pinch/grab/open palm/swipe/scale/rotate via MediaPipe/OpenCV |
| Gesture-to-HUD proposals | VERIFIED_CI_CHECKPOINT | safe typed proposals; no direct OS bypass |
| Physical camera QR/object perception | IMPLEMENTED_NEEDS_HARDWARE | `camera_vision.py`, `vision_yolo.py` |
| Continuous camera recording | NOT_USED | camera design is explicit-session/local; raw frame persistence is not enabled by default |

## Memory / world model / natural language

| Area | Status | Evidence / boundary |
|---|---|---|
| Layered persistent memory | VERIFIED_CI_CHECKPOINT | working/episodic/semantic/procedural memory |
| Personal World Model | VERIFIED_CI_CHECKPOINT | persistent source/confidence-aware world facts |
| Relationship graph | VERIFIED_CI_CHECKPOINT | `world_graph.py`; project → repo → bug → person/deadline/file relationships |
| Context-aware language | VERIFIED_CI_CHECKPOINT | pronouns, previous/other/same/again references plus multimodal targets |
| Procedure learning by demonstration | VERIFIED_CI_CHECKPOINT | semantic steps, parameterization, sensitive-field exclusion, owner verification before replay |
| Predictive behavior | VERIFIED_CI_CHECKPOINT | bounded routine learning; suggestion-only, reversible by default |

## Autonomous intelligence

| Area | Status | Evidence / boundary |
|---|---|---|
| Persistent missions | VERIFIED_CI_CHECKPOINT | resumable DAG execution, retries and checkpoints |
| Bounded mission replanning/recovery | VERIFIED_CI_CHECKPOINT | `advanced_missions.py` |
| Specialized multi-agent council | VERIFIED_CI_CHECKPOINT | Planner/Researcher/Engineer/Critic/Safety/Verifier; non-voting and fail-closed |
| Council-gated mission verification | IMPLEMENTED_NEEDS_CURRENT_CI | `mission_intelligence.py`; verifier receives actual terminal task evidence |
| Proactive awareness | VERIFIED_CI_CHECKPOINT | world/mission events, cooldown/rate limiting, dismissal memory |
| Contextual interruption policy | VERIFIED_CI_CHECKPOINT | urgency/importance/conversation/presentation/DND/quiet-hours/mission priority |
| Research evidence intelligence | VERIFIED_CI_CHECKPOINT | citation/source ledger, credibility weighting, contradiction detection |

## Identity / security

| Area | Status | Evidence / boundary |
|---|---|---|
| Wake phrase vs speaker identity separation | VERIFIED_CI_CHECKPOINT | wake detection never proves owner identity |
| Speaker embedding matching | IMPLEMENTED_NEEDS_HARDWARE | local sherpa-onnx embedding extraction + cosine matching |
| Owner/guest/unknown roles | VERIFIED_CI_CHECKPOINT | speaker trust policy |
| Contextual security scoring | VERIFIED_CI_CHECKPOINT | speaker + session + trusted device + presence + strong auth |
| Windows Hello / PIN consent boundary | IMPLEMENTED_NEEDS_HARDWARE | lazy WinRT `UserConsentVerifier`; PANGU receives result, not biometric templates |

## Windows / devices / UI

| Area | Status | Evidence / boundary |
|---|---|---|
| Application/window/system audio/brightness control | VERIFIED_CI_CHECKPOINT | deterministic adapters and postcondition evidence where supported |
| Extended Windows information/control | VERIFIED_CI_CHECKPOINT | network, Wi-Fi profiles, Bluetooth/PnP, printers, services, startup apps, clipboard, processes, device health, allowlisted Settings pages |
| Native spatial HUD | VERIFIED_CI_CHECKPOINT_NEEDS_DISPLAY_VALIDATION | multi-monitor WinForms overlay, Per-Monitor-V2 DPI, waveform, cards, telemetry, notices, gesture targets, state bridge |
| Windows Session Agent | IMPLEMENTED_NEEDS_CURRENT_CI_AND_LOGIN_VALIDATION | tray supervisor, backend/overlay health, restart backoff, rapid-crash circuit break, unhealthy-running backend recovery |
| Phone/wearable ecosystem | ADAPTER_FOUNDATION | trusted-device/capability registry and safe Home Assistant path exist; calls/messages/wearables need real paired endpoints and confirmation flows |
| Full virtual-desktop/taskbar/private Windows APIs | PARTIAL | stable deterministic coverage exists for broad Windows controls; unsupported/private APIs are not faked |

## Offline / coding / self-upgrade

| Area | Status | Evidence / boundary |
|---|---|---|
| Offline deterministic operation | VERIFIED_CI_CHECKPOINT | deterministic Windows/system functions do not require Gemini |
| Optional local reasoning fallback | IMPLEMENTED_NEEDS_MODEL_VALIDATION | llama.cpp GGUF adapter; no Ollama, no silent download |
| Repository semantic/coding intelligence | VERIFIED_CI_CHECKPOINT | AST/symbol indexing, impact analysis, test discovery, failure diagnosis |
| Isolated owner-directed self-upgrade | VERIFIED_CI_CHECKPOINT | worktree, protected paths, diff check, tests, branch/backup/rollback boundaries |
| Benchmark-gated CLI promotion | IMPLEMENTED_NEEDS_CURRENT_CI | exact base/candidate revision artifacts required before `--apply`; protected metrics may not regress beyond policy |

## Reliability claim

PANGU is designed to be **failure-isolating and self-recovering**, not literally uncrashable. Software cannot guarantee "never crash." The production design instead combines bounded queues, bulkheads, circuit breakers, load shedding, timeouts, retries, component health probes, in-process recovery, Session Agent process supervision, crash-loop circuit breaking, checkpoints and rollback.

## Required real target-machine validation

The following cannot be truthfully certified by GitHub CI alone:

- wake false-accept/false-reject rates under real room noise and PANGU speaker playback;
- Faster Whisper WER and latency on the installed model/hardware;
- continuous barge-in, speaker echo, microphone device loss/reconnect and long-session audio behavior;
- Tesseract OCR quality on real applications and DPI/scaling combinations;
- YOLO/camera, QR, gestures, two-hand spatial interaction and speaker embeddings;
- Windows Hello availability and user-verification flow;
- HUD multi-monitor placement, click-through, DPI changes and gesture targeting;
- Session Agent login startup, crash recovery and sleep/resume behavior;
- real Bluetooth/Wi-Fi/printer/audio/display hardware variants;
- paired phone/wearable/smart-home endpoints;
- end-to-end benchmark numbers used by self-upgrade promotion.

## Validation gates

`.github/workflows/ci.yml` runs the Windows software gate through `scripts/test.ps1`: Python compileall, Ruff lint, Ruff formatting, strict mypy, pytest and `dotnet test Pangu.sln`.

A prior advanced-integration checkpoint through commit `a50f4f41eb5cac3c35734424f8c078399b8b0f0a` completed PANGU CI successfully. Newer commits must be treated as pending until their own workflow run completes.
