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


## Persistence continuation — 2026-08-05

Alembic head is `0002_persistent_exact_approval`. `0001` owns the base thirteen runtime tables and `0002` expands the existing `approvals` table; production DDL remains Alembic-only. `DatabaseService` remains the sole engine owner and `persistence.py` is an import-compatibility facade.

`repositories.py` provides domain records and session-owned SQLAlchemy repositories. `approvals.py` provides canonical SHA-256-bound persistent approvals. One-time consumption uses a conditional update and creates its consumption history in the same transaction; revocation and revocation history are likewise atomic. Canonicalization sorts mapping keys, sets, and permission scopes while preserving list order, and emits UTC timestamps.

Validation: 33 Python tests passed; formatting, Ruff, mypy, and compileall pass.
# Gemini provider reliability

The production model layer now has an SDK-isolated Google transport, fake transport tests, bounded retries, circuit breaker, per-mission budgets, privacy sanitization, structured JSON repair, and model capability routing. No live Gemini health check has been claimed or performed by automated tests.
