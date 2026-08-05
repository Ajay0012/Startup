# Manual validation

1. Run `scripts/bootstrap.ps1`, then `scripts/test.ps1`.
2. Run `scripts/development.ps1`; call `GET http://127.0.0.1:8765/health` and confirm no public binding.
3. With a test workspace, run `pangu "create folder reports"` and verify the folder and SQLite audit record.
4. Install Windows App SDK, then replace the degraded overlay host with a WinUI host and test click-through, DPI, and multi-monitor behavior interactively.
5. Add a valid Gemini key locally and run a health check; never copy the key to a report.
6. Connect a microphone and test wake-word, VAD, TTS echo suppression, and speech lifecycle using a real `pangu.onnx` model.

## Database lifecycle validation

Run `GET /health` to inspect only sanitized database state. `GET /ready` returns 200 only after the lifecycle startup has migrated and admitted database work; it returns 503 otherwise. Run `python -m pytest -q -p no:cacheprovider` with a writable temporary directory.
