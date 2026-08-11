from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PhoneCapability(StrEnum):
    AUTHENTICATE = "authenticate"
    PLACE_CALL = "place_call"
    ANSWER_CALL = "answer_call"
    END_CALL = "end_call"
    CALL_STATE = "call_state"
    CALL_MEDIA = "call_media"
    NOTIFICATIONS = "notifications"
    CONTACT_LOOKUP = "contact_lookup"
    CONTEXT_SYNC = "context_sync"


class PhoneCommand(StrEnum):
    AUTHENTICATE = "authenticate"
    PLACE_CALL = "place_call"
    ANSWER_CALL = "answer_call"
    END_CALL = "end_call"
    SPEAK = "speak"
    PAUSE_SPEECH = "pause_speech"
    RESUME_SPEECH = "resume_speech"
    QUERY_CALL = "query_call"
    LOOKUP_CONTACT = "lookup_contact"


@dataclass(frozen=True)
class PhoneLinkMessage:
    device_id: str
    sequence: int
    kind: str
    issued_at: int
    expires_at: int
    payload: dict[str, object]
    signature: str


@dataclass(frozen=True)
class ConnectedPhone:
    device_id: str
    capabilities: frozenset[PhoneCapability]
    connected_at: float
    last_seen: float
    authenticated_until: float | None = None


@dataclass(frozen=True)
class CommandLease:
    command_id: str
    command: PhoneCommand
    payload: dict[str, object]
    issued_at: int
    expires_at: int
    requires_device_auth: bool


class PhoneLinkRuntime:
    """Single replay-safe companion link owner for PANGU.

    Network transport is supplied by the existing FastAPI backend. This runtime owns
    authentication, sequence validation, device capabilities and bounded outbound command
    leases. Pairing secrets are configuration inputs and never published to events/logs.
    """

    def __init__(self, secret: str | None, *, command_ttl_seconds: int = 30) -> None:
        if not 5 <= command_ttl_seconds <= 300:
            raise ValueError("phone command TTL must be between 5 and 300 seconds")
        self._secret = secret.encode("utf-8") if secret else None
        if self._secret is not None and len(self._secret) < 32:
            raise ValueError("phone pairing secret must be at least 32 characters")
        self.command_ttl_seconds = command_ttl_seconds
        self._phone: ConnectedPhone | None = None
        self._inbound_sequence = -1
        self._outbound_sequence = 0
        self._queue: deque[CommandLease] = deque(maxlen=128)

    @property
    def configured(self) -> bool:
        return self._secret is not None

    @property
    def phone(self) -> ConnectedPhone | None:
        return self._phone

    @staticmethod
    def _canonical(
        device_id: str,
        sequence: int,
        kind: str,
        issued_at: int,
        expires_at: int,
        payload: dict[str, object],
    ) -> bytes:
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        return "\n".join(
            (device_id, str(sequence), kind, str(issued_at), str(expires_at), digest)
        ).encode("utf-8")

    def _sign(
        self,
        device_id: str,
        sequence: int,
        kind: str,
        issued_at: int,
        expires_at: int,
        payload: dict[str, object],
    ) -> str:
        if self._secret is None:
            raise RuntimeError("PHONE_LINK_NOT_CONFIGURED")
        return hmac.new(
            self._secret,
            self._canonical(device_id, sequence, kind, issued_at, expires_at, payload),
            hashlib.sha256,
        ).hexdigest()

    def verify(self, message: PhoneLinkMessage, *, now: int | None = None) -> bool:
        if self._secret is None or not message.device_id.strip():
            return False
        current = int(time.time()) if now is None else now
        if message.expires_at < current or message.issued_at > current + 30:
            return False
        if message.expires_at - message.issued_at > 300:
            return False
        if message.sequence <= self._inbound_sequence:
            return False
        expected = self._sign(
            message.device_id,
            message.sequence,
            message.kind,
            message.issued_at,
            message.expires_at,
            message.payload,
        )
        if not hmac.compare_digest(expected, message.signature):
            return False
        self._inbound_sequence = message.sequence
        return True

    def parse_and_verify(self, raw: str, *, now: int | None = None) -> PhoneLinkMessage | None:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("payload"), dict):
                return None
            message = PhoneLinkMessage(
                device_id=str(payload["device_id"]),
                sequence=int(payload["sequence"]),
                kind=str(payload["kind"]),
                issued_at=int(payload["issued_at"]),
                expires_at=int(payload["expires_at"]),
                payload=dict(payload["payload"]),
                signature=str(payload["signature"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return message if self.verify(message, now=now) else None

    def connect(self, device_id: str, capabilities: frozenset[PhoneCapability]) -> ConnectedPhone:
        if not self.configured:
            raise RuntimeError("PHONE_LINK_NOT_CONFIGURED")
        clean = device_id.strip()
        if not clean or len(clean) > 200:
            raise ValueError("invalid phone device id")
        now = time.monotonic()
        self._phone = ConnectedPhone(clean, capabilities, now, now)
        self._inbound_sequence = -1
        self._outbound_sequence = 0
        self._queue.clear()
        return self._phone

    def disconnect(self) -> None:
        self._phone = None
        self._queue.clear()

    def touch(self) -> None:
        if self._phone is None:
            return
        self._phone = ConnectedPhone(
            self._phone.device_id,
            self._phone.capabilities,
            self._phone.connected_at,
            time.monotonic(),
            self._phone.authenticated_until,
        )

    def mark_authenticated(self, *, seconds: float = 120.0) -> None:
        if self._phone is None:
            raise RuntimeError("PHONE_NOT_CONNECTED")
        if not 15 <= seconds <= 600:
            raise ValueError("authentication lease must be between 15 and 600 seconds")
        self._phone = ConnectedPhone(
            self._phone.device_id,
            self._phone.capabilities,
            self._phone.connected_at,
            time.monotonic(),
            time.monotonic() + seconds,
        )

    def has_fresh_authentication(self) -> bool:
        return (
            self._phone is not None
            and self._phone.authenticated_until is not None
            and self._phone.authenticated_until >= time.monotonic()
        )

    def queue_command(
        self,
        command: PhoneCommand,
        payload: dict[str, object],
        *,
        capability: PhoneCapability,
        requires_device_auth: bool = False,
    ) -> CommandLease:
        phone = self._phone
        if phone is None:
            raise RuntimeError("PHONE_NOT_CONNECTED")
        if capability not in phone.capabilities:
            raise PermissionError(f"phone does not expose {capability.value}")
        if requires_device_auth and not self.has_fresh_authentication():
            raise PermissionError("FRESH_DEVICE_AUTH_REQUIRED")
        issued = int(time.time())
        lease = CommandLease(
            secrets.token_urlsafe(18),
            command,
            dict(payload),
            issued,
            issued + self.command_ttl_seconds,
            requires_device_auth,
        )
        self._queue.append(lease)
        return lease

    def next_wire_command(self) -> dict[str, Any] | None:
        phone = self._phone
        if phone is None:
            return None
        now = int(time.time())
        while self._queue and self._queue[0].expires_at < now:
            self._queue.popleft()
        if not self._queue:
            return None
        lease = self._queue.popleft()
        self._outbound_sequence += 1
        payload: dict[str, object] = {
            "command_id": lease.command_id,
            "command": lease.command.value,
            "arguments": dict(lease.payload),
            "requires_device_auth": lease.requires_device_auth,
        }
        signature = self._sign(
            phone.device_id,
            self._outbound_sequence,
            "command",
            lease.issued_at,
            lease.expires_at,
            payload,
        )
        return {
            "device_id": phone.device_id,
            "sequence": self._outbound_sequence,
            "kind": "command",
            "issued_at": lease.issued_at,
            "expires_at": lease.expires_at,
            "payload": payload,
            "signature": signature,
        }
