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


def signed_message(runtime: PhoneLinkRuntime, sequence: int, *, now: int = 1000) -> PhoneLinkMessage:
    payload: dict[str, object] = {"capabilities": ["place_call"]}
    signature = runtime._sign(  # noqa: SLF001 - regression test covers the protocol primitive.
        "phone-1", sequence, "heartbeat", now, now + 30, payload
    )
    return PhoneLinkMessage("phone-1", sequence, "heartbeat", now, now + 30, payload, signature)


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
    expected = runtime._sign(  # noqa: SLF001
        parsed["device_id"],
        parsed["sequence"],
        parsed["kind"],
        parsed["issued_at"],
        parsed["expires_at"],
        payload,
    )
    assert parsed["signature"] == expected
    assert parsed["expires_at"] - parsed["issued_at"] == 20
