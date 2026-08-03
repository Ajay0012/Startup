# Requirements traceability

| Requirement group | Sources | Tests | Status |
|---|---|---|---|
| Command envelope, normalization, result state | `contracts.py`, `language.py`, `runtime.py` | `tests/test_runtime.py` | IMPLEMENTED_UNVERIFIED |
| Local persistence/audit | `persistence.py` | `tests/test_runtime.py` | IMPLEMENTED_UNVERIFIED |
| Safety and exact-operation primitive | `security.py`, `tools.py` | pending dependency bootstrap | IMPLEMENTED_UNVERIFIED |
| File creation + postcondition | `tools.py` | `tests/test_runtime.py` | IMPLEMENTED_UNVERIFIED |
| Local API boundary | `apps/backend/main.py` | pending FastAPI install | IMPLEMENTED_UNVERIFIED |
| Session mutex / overlay IPC contracts | `apps/session-agent`, `apps/overlay-contracts` | pending .NET build | IMPLEMENTED_UNVERIFIED |

All unlisted specification requirements are `NOT_STARTED` or `BLOCKED_ENVIRONMENT`; they are not represented as complete.
