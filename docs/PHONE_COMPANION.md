# PANGU Android Companion and Delegated Calling

## Goal

Connect an owner-controlled Android phone to PANGU without turning the phone into an unrestricted remote-control endpoint. The companion can expose bounded capabilities such as authenticated call placement, incoming-call management, notification/context reads and supported assistant-managed call media.

PANGU must never bypass Android lock-screen authentication, extract credentials, silently record calls, or assume access to ordinary carrier-call audio.

## Supported capability model

The companion advertises capabilities during each authenticated link session:

- `authenticate` — show Android biometric/device-credential UI and return only success/failure.
- `place_call` — place an outgoing call after PANGU policy and device-auth requirements pass.
- `answer_call` — answer a known incoming call when the owner has approved it or an explicit owner rule permits it.
- `end_call` — terminate a known call.
- `call_state` — return bounded call metadata/state.
- `notifications` — return only owner-approved notification fields/apps.
- `contact_lookup` — resolve a saved contact locally on the phone; do not synchronize the full address book by default.
- `context_sync` — send an allowlisted subset of phone context to PANGU.
- `call_media` — optional and **not implied by default-dialer status**. Advertise only when the companion owns a supported media path such as app-managed VoIP/WebRTC.

## Android platform roles

### Default dialer

To manage ordinary Android Telecom calls, the companion should request `RoleManager.ROLE_DIALER`, implement `InCallService`, and provide the required dial/in-call UI. The user must grant this role in Android system UI. PANGU cannot silently grant it.

Default-dialer mode can provide call placement, answer, disconnect and call-state management through Android Telecom APIs. Emergency calls remain outside autonomous PANGU policy.

### Device authentication

Privileged phone actions use Android's system biometric/device-credential prompt. PANGU requests authentication with a human-readable reason; the companion shows the system prompt in the foreground and returns only an authenticated/not-authenticated result. PANGU never receives a PIN, pattern, password, face template or fingerprint data.

This is an authentication gate, not a lock-screen bypass. If Android requires the owner to unlock/interact with system UI, PANGU waits.

## Secure link protocol

`src/pangu/phone_link.py` owns PANGU-side message verification and command leases.

Every link message includes:

- paired `device_id`;
- strictly increasing sequence number;
- message kind;
- issue and expiration timestamps;
- structured payload;
- HMAC-SHA256 signature using the pairing secret.

Security properties:

- replayed sequence numbers are rejected;
- expired commands are rejected;
- commands are short-lived leases;
- the phone advertises capabilities every session;
- a capability cannot be invoked if the phone did not advertise it;
- privileged commands may require a fresh device-auth lease;
- the pairing secret is configuration/secure-storage material and must not be published to logs/events;
- losing connectivity invalidates queued commands.

For production, use TLS or a trusted private tunnel between PC and phone. Do not expose the PANGU backend directly to the public Internet.

## Calling another person

A request such as:

> Hey PANGU, call Arun.

is resolved in these stages:

1. Resolve the contact locally on the phone where possible.
2. Present/obtain required owner confirmation and fresh device authentication according to policy.
3. Send a short-lived `place_call` lease containing the resolved number/contact identifier.
4. The phone verifies the lease and checks that it is still the active paired device.
5. The companion asks Android Telecom to place the call.
6. PANGU observes call state but does not automatically gain access to call audio.

Emergency-number calls are never delegated autonomously.

## Incoming calls

An incoming call can be classified locally using an owner-defined rule set, for example:

- always ask before answering unknown callers;
- never auto-answer private/blocked numbers;
- allow auto-answer only from explicitly allowlisted contacts while the owner is present;
- never auto-answer while the device is in a sensitive context;
- always show a visible notification that PANGU is managing the call.

If no owner rule grants authority, `answer_call` requires confirmation.

## Autonomous appointment/reservation calls

`src/pangu/phone_delegation.py` provides a deterministic policy envelope around the conversation.

The owner defines constraints before the call, for example:

- purpose: book a general physician appointment;
- target: Example Hospital;
- accepted date: 12 August 2026;
- accepted time: 10:00–12:00;
- approved provider(s): Dr Rao;
- approved branch(es): Main Branch;
- maximum fee: INR 1,500;
- no payment without confirmation;
- no medical/personal disclosure without confirmation.

PANGU may negotiate dynamically inside that envelope. A change already inside the envelope can continue without interrupting the owner. A material change outside the envelope creates a one-time exact proposal token and pauses the mission.

Examples that require owner confirmation by default:

- moving to a different day or outside an approved time window;
- changing branch/provider outside the allowlist;
- higher or unverifiable price;
- payment/deposit requests;
- cancellation;
- personal identity information beyond the approved minimum;
- medical/sensitive information;
- any unclassified material commitment.

`src/pangu/phone_orchestrator.py` publishes a redacted `phone.call.confirmation_required` event to the shared EventBus. HUD/voice/mobile UI can ask the owner. After approve/decline, the same call mission resumes with the exact proposal token.

## Assistant disclosure

When PANGU itself speaks to another person, the default policy requires a brief disclosure that the caller is an automated assistant acting on behalf of the user. The system must not pretend to be the owner.

## Carrier calls vs assistant-managed media

Android default-dialer privileges do not automatically mean PANGU can inject synthesized audio into every carrier call or capture both call directions. Therefore two modes are intentionally separate:

1. **Carrier call control** — dial, answer, hang up, observe state. This uses Android Telecom capabilities.
2. **Delegated conversation media** — PANGU speaks/listens only when the companion advertises `call_media`, such as an application-owned VoIP/WebRTC/telephony gateway where the app legitimately controls the media stream.

If `call_media` is not advertised, PANGU must fail closed instead of claiming it can speak autonomously on that carrier call.

## Privacy defaults

- No raw call audio retention.
- No full transcript retention by default.
- Sensitive proposal summaries are redacted before EventBus publication/history.
- Contact lookup is local-first; full contact-list synchronization is disabled by default.
- Notifications are allowlisted and bounded.
- Authentication secrets/biometric data never enter PANGU.
- Pairing can be revoked at any time.
- Command leases are short-lived and replay protected.
- Consequential actions remain auditable.

## Recommended advanced features

The phone subsystem is designed to support these additions without weakening the policy boundary:

- owner-presence sensing before auto-answer;
- trusted Bluetooth/watch proximity as a contextual signal, never sole authentication;
- quiet-hours and driving-mode policies;
- VIP/unknown/spam call routing;
- local spam scoring without uploading the address book;
- live call summary shown on the PANGU HUD;
- real-time owner intervention: `take over`, `stop`, `decline`, `accept`, `ask for another slot`;
- automatic callback scheduling after busy/no-answer;
- multilingual call dialogue with fixed policy decisions underneath;
- DTMF/IVR navigation with explicit limits;
- confirmation receipts containing provider, date/time, price and booking reference;
- post-call semantic memory containing only the minimum booking facts;
- temporary delegation scopes that expire after one mission;
- separate work/personal profiles and contact policies;
- hardware-backed pairing keys in Android Keystore and Windows credential storage;
- optional private VoIP/WebRTC media for full assistant conversation.

## Validation status

The PANGU-side secure paired-device adapter, replay-safe link protocol, exact confirmation gate, policy-bound delegated-call session and EventBus confirmation orchestrator are implemented with regression tests.

Actual Android default-dialer behavior, biometric prompts, phone hardware, carrier behavior and assistant-managed VoIP media require the companion application and real-device validation. Do not mark those paths hardware-verified until exercised on the target phone/network.
