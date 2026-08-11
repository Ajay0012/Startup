# PANGU Android Companion

This directory contains the handset-side foundation for the PANGU phone subsystem.

## Implemented source

- `PanguDialerActivity` requests the Android default-dialer role through system UI.
- `PanguInCallService` receives Android Telecom calls when the app holds that role.
- `CallRegistry` keeps only live `Call` objects and exposes bounded answer/disconnect/state operations.
- `PhoneSecurityLease` is an in-memory, expiring privilege lease. Process restart/reboot clears it.
- `PanguCommandDispatcher` checks default-dialer role, phone permission, fresh auth lease and emergency-number exclusion before placing a call; answer/end calls are similarly gated.
- `PanguCommandDispatcher.speakOnCarrierCall()` deliberately returns `CARRIER_CALL_MEDIA_NOT_EXPOSED` rather than pretending ordinary carrier audio is available to the assistant.

## Important current boundary

The Android system biometric/device-credential Activity is **not committed yet**. Until it is implemented and tested on the target handset, `PhoneSecurityLease` is never legitimately granted by the companion and privileged calls fail closed.

Do not replace that gate with a hidden PIN, stored password, Accessibility-based lock bypass, ADB unlock command, root shell, or any other credential-bypass mechanism.

## Build integration still required

This source foundation is intentionally not yet registered in the repository's Windows-only solution/CI. Before producing an APK, create the Android Gradle module with:

- a supported current Android SDK/minSdk;
- AndroidX Activity/Core;
- AndroidX Biometric for the user-authentication screen;
- a secure outbound transport client for the PANGU phone-link protocol;
- Android Keystore storage for the pairing key;
- visible foreground-service/notification behavior when the companion link must remain active.

The APK should not request unrelated permissions.

## Recommended test order

1. Install debug APK on the owner's test handset.
2. Grant PANGU Companion the default-dialer role using Android system UI.
3. Verify normal incoming/outgoing calls still work without PANGU automation.
4. Implement and test the system biometric/device-credential gate.
5. Pair the phone to PANGU with a generated 256-bit-or-stronger secret stored in Android Keystore/PC secret storage.
6. Verify signed command expiry and replay rejection.
7. Verify call placement to a non-emergency test number.
8. Verify answer/end actions on a test call.
9. Verify emergency numbers are rejected by autonomous command handling.
10. Verify no call audio/transcript is retained by default.
11. Add assistant-managed VoIP/WebRTC media separately before enabling autonomous spoken booking calls.

See `docs/PHONE_COMPANION.md` for the architecture, policy envelope and privacy rules.
