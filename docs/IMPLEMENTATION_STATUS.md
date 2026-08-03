# Implementation status

| Area | Status | Evidence |
|---|---|---|
| Deterministic command and SQLite audit slice | VERIFIED_AUTOMATED | `runtime.py`, `persistence.py`, pytest |
| Bounded EventBus | VERIFIED_AUTOMATED | `events.py`; lifecycle integration tests pending |
| Lifecycle Kernel | VERIFIED_AUTOMATED | `lifecycle.py`; integration tests pending |
| Capability catalog and scoped permissions | VERIFIED_AUTOMATED | `capabilities.py`, `permissions.py`, `tools.py` |
| Exact approval primitive | VERIFIED_AUTOMATED | `security.py`, `tools.py` |
| Safe filesystem create/write/recycle boundary | VERIFIED_AUTOMATED | `filesystem.py`, `tools.py` |
| Local API | VERIFIED_AUTOMATED | `apps/backend/main.py` |
| Native session/overlay foundations | IMPLEMENTED_UNVERIFIED | .NET projects build independently |
| Gemini, voice, browser, Windows control | BLOCKED_ENVIRONMENT | physical/integration implementation remains outstanding |

