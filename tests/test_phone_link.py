from __future__ import annotations

import json

import pytest

from pangu.phone_link import (
    PhoneCapability,
    PhoneCommand,
    PhoneLinkMessage,
    PhoneLinkRuntime,
)

SECRET = "s" * 64


def signed_message(
    runtime: PhoneLinkRuntime,
    sequence: int,
    *,
    now: int = 1000,
    kind: str = "heartbeat",
    payload: dict[str, object] | None = None,
) -> PhoneLinkMessage:
    body = payload or {"capabilities": ["place_call"]}
    signature = runtime._sign("phone-1", sequence, kind, now, now + 30, body)
    return PhoneLinkMessage("phone-1", sequence, kind, now, now + 30, body, signature)


def test_phone_link_rejects_replay_and_expired_messages() -> None:
    runtime = PhoneLinkRuntime(SECRET)
    message = signed_message(runtime, 1)
    assert runtime.verify(message, now=1000)
    assert not runtime.verify(message, now=1000)
    expired = signed_message(runtime, 2, now=900)
    assert not runtime.verify(expired, now=1000)


def test_phone_link_rejects_tampered_payload() -> None:
    runtime = PhoneLinkRuntime(SECRET)
    message = signed_message(runtime, 1)
    tampered = PhoneLinkMessage(
        message.device_id,
        message.sequence,
        message.kind,
        message.issued_at,
        message.expires_at,
        {"capabilities": ["answer_call"]},
        message.signature,
    )
    assert not runtime.verify(tampered, now=1000)


def test_pairing_challenge_is_one_time_and_hello_sequence_remains_replay_floor() -> None:
    runtime = PhoneLinkRuntime(SECRET)
    challenge = runtime.issue_pairing_challenge(now=1000)
    hello = signed_message(
        runtime,
        10,
        now=1000,
        kind="hello",
        payload={
            "challenge": challenge.challenge,
            "capabilities": ["authenticate", "place_call", "call_media"],
        },
    )
    assert runtime.verify(hello, now=1000)
    phone = runtime.accept_hello(hello, now=1000)
    assert phone.device_id == "phone-1"
    assert PhoneCapability.CALL_MEDIA in phone.capabilities

    # The hello signature cannot be replayed and a lower sequence cannot be accepted later.
    assert not runtime.verify(hello, now=1000)
    assert not runtime.verify(signed_message(runtime, 9, now=1000), now=1000)

    second = signed_message(
        runtime,
        11,
        now=1000,
        kind="hello",
        payload={"challenge": challenge.challenge, "capabilities": ["place_call"]},
    )
    assert runtime.verify(second, now=1000)
    with pytest.raises(PermissionError, match="PAIRING_CHALLENGE_INVALID"):
        runtime.accept_hello(second, now=1000)


def test_phone_link_requires_capability_and_fresh_auth_for_privileged_command() -> None:
    runtime = PhoneLinkRuntime(SECRET)
    runtime.connect(
        "phone-1",
        frozenset({PhoneCapability.PLACE_CALL, PhoneCapability.AUTHENTICATE}),
    )
    with pytest.raises(PermissionError, match="FRESH_DEVICE_AUTH_REQUIRED"):
        runtime.queue_command(
            PhoneCommand.PLACE_CALL,
            {"number": "+911234567890"},
            capability=PhoneCapability.PLACE_CALL,
            requires_device_auth=True,
        )
    runtime.mark_authenticated(seconds=60)
    lease = runtime.queue_command(
        PhoneCommand.PLACE_CALL,
        {"number": "+911234567890"},
        capability=PhoneCapability.PLACE_CALL,
        requires_device_auth=True,
    )
    assert lease.command == PhoneCommand.PLACE_CALL
    wire = runtime.next_wire_command()
    assert wire is not None
    assert wire["payload"]["command"] == "place_call"  # type: ignore[index]


def test_phone_link_wire_command_is_signed_and_bounded() -> None:
    runtime = PhoneLinkRuntime(SECRET, command_ttl_seconds=20)
    runtime.connect("phone-1", frozenset({PhoneCapability.END_CALL}))
    runtime.queue_command(
        PhoneCommand.END_CALL,
        {"call_id": "abc"},
        capability=PhoneCapability.END_CALL,
    )
    wire = runtime.next_wire_command()
    assert wire is not None
    raw = json.dumps(wire)
    parsed = json.loads(raw)
    payload = parsed["payload"]
    expected = runtime._sign(
        parsed["device_id"],
        parsed["sequence"],
        parsed["kind"],
        parsed["issued_at"],
        parsed["expires_at"],
        payload,
    )
    assert parsed["signature"] == expected
    assert parsed["expires_at"] - parsed["issued_at"] == 20
