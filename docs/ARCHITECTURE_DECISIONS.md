# Architecture decisions

1. A Python modular monolith owns command execution. Tools are reached only by `Runtime -> ToolRuntime` after deterministic safety classification.
2. SQLite is the current authoritative local audit store; WAL and foreign-key enforcement are activated at startup.
3. Cloud reasoning is optional and must remain behind a provider boundary. No key is printed or persisted by this implementation.
4. Native processes are separate .NET executables. The current overlay reports degraded capability rather than pretending to render.

## Persistence continuation

The authoritative local persistence boundary is `DatabaseService`; compatibility `persistence.Database` delegates to it and owns no connection, engine, or schema. Revision `0002_persistent_exact_approval` adds the fields needed to bind approvals to actor, tool/version, operation, canonical arguments, target, risk, scopes, mission/session, expiry, and approval mode. Exact-operation hashes are SHA-256 of canonical JSON.
# ADR: Gemini is an injected transport

Only `GoogleGenAITransport` imports the official SDK, and it imports it lazily. RuntimeBuilder owns provider construction; API and CLI only consume the shared container.
