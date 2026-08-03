# PANGU AI

PANGU AI is a local-first, safety-controlled Windows operating layer. This repository delivers a runnable deterministic command pipeline, SQLite audit/memory store, exact approvals, loopback API boundary, Windows adapter boundaries, and native-process foundations for the session agent and overlay.

## Quick start

```powershell
./scripts/bootstrap.ps1
./scripts/development.ps1
py -3.12 -m pangu.cli "create folder reports"
```

The runtime is intentionally useful without Gemini. Add `GEMINI_API_KEY` to the local `.env` to enable the isolated provider when its optional package is installed.
