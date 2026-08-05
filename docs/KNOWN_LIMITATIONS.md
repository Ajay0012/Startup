# Known limitations

The implementation intentionally does not claim unavailable integrations. `google-genai`, FastAPI, SQLAlchemy, Playwright, audio/ONNX, and Windows UI Automation packages have not been installed in this execution. Gemini also requires a user-provided key. The WinUI 3 / Windows App SDK workload and pangu.onnx model are absent. WMI GPU and audio discovery was denied. The native projects are contract-capable .NET foundations, not a verified WinUI visual renderer.

## Persistence lifecycle limitation

The database lifecycle and structured health endpoint are automated with SQLite. Failure injection, broad concurrent worker stress, and production process shutdown behavior still require further integration coverage before a full persistence-layer completion claim can be made.
# Gemini transport limitation

Automated validation uses `FakeGeminiTransport`; it does not perform network calls or assert a live Gemini account is healthy. A configured deployment must validate model availability separately.
