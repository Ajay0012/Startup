# Architecture decisions

1. A Python modular monolith owns command execution. Tools are reached only by `Runtime -> ToolRuntime` after deterministic safety classification.
2. SQLite is the current authoritative local audit store; WAL and foreign-key enforcement are activated at startup.
3. Cloud reasoning is optional and must remain behind a provider boundary. No key is printed or persisted by this implementation.
4. Native processes are separate .NET executables. The current overlay reports degraded capability rather than pretending to render.
