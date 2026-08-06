# Windows system-control runtime

PANGU exposes verified, capability-gated master audio, mute, and supported-display brightness controls. Public values are integer percentages (0–100); requested percentages are clamped, while malformed values are rejected. Each mutation reads state, applies the native operation, rereads it, and only reports `VERIFIED` after the requested state is observed.

Capabilities are `system.audio.read`, `system.audio.write`, `system.brightness.read`, and `system.brightness.write`. Reads and low-risk writes are audited and need no approval under the default policy; a future policy can require approval without changing an adapter.

CLI examples: `python -m pangu system volume`, `python -m pangu system volume set 50`, `python -m pangu system mute toggle`, and `python -m pangu system brightness set 60 --display display-1`. API endpoints are `GET /system/audio`, `POST /system/audio/volume`, `POST /system/audio/mute`, `GET /system/brightness`, and `POST /system/brightness`.

Brightness is only available where the native binding can enumerate and change a compatible display. Multiple controllable displays require the sanitized selector returned by the read operation. Native dependencies unavailable, no endpoint, access denied, and unobserved postconditions fail closed; no desktop validation has yet been claimed.

Manual Windows validation: install compatible local Core Audio and brightness bindings, then run the CLI examples above; confirm the desktop state changes and the JSON result reports `VERIFIED`. Run `python -m pangu system brightness` first and use only its returned `display-*` selector.
