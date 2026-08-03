# Implementation status

| Area | Status | Evidence |
|---|---|---|
| Deterministic command and SQLite audit slice | VERIFIED_AUTOMATED | `runtime.py`, `persistence.py`, pytest |
| Bounded EventBus | IMPLEMENTED_UNVERIFIED | `events.py`; lifecycle integration tests pending |
| Lifecycle Kernel | IMPLEMENTED_UNVERIFIED | `lifecycle.py`; integration tests pending |
| Capability catalog and scoped permissions | IMPLEMENTED_UNVERIFIED | `capabilities.py`, `permissions.py`, `tools.py` |
| Exact approval primitive | IMPLEMENTED_UNVERIFIED | `security.py`, `tools.py` |
| Safe filesystem create/write/recycle boundary | IMPLEMENTED_UNVERIFIED | `filesystem.py`, `tools.py` |
| Local API | VERIFIED_AUTOMATED | `apps/backend/main.py` |
| Native session/overlay foundations | IMPLEMENTED_UNVERIFIED | .NET projects build independently |
| Gemini, voice, browser, Windows control | BLOCKED_ENVIRONMENT | physical/integration implementation remains outstanding |
