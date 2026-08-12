from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urlparse

from .device_ecosystem import DeviceActionResult


@dataclass(frozen=True)
class PairedDevicePolicy:
    timeout_seconds: float = 5.0
    maximum_response_bytes: int = 1_000_000
    require_https: bool = True

    def __post_init__(self) -> None:
        if not 0.5 <= self.timeout_seconds <= 30:
            raise ValueError("device timeout must be between 0.5 and 30 seconds")
        if not 1024 <= self.maximum_response_bytes <= 10_000_000:
            raise ValueError("device response bound is invalid")


class PairedDeviceSigner:
    """HMAC-sign local device requests; the shared secret never leaves this boundary."""

    def __init__(self, device_id: str, secret: bytes) -> None:
        if not device_id.strip() or len(secret) < 32:
            raise ValueError("paired device requires an id and at least 256 bits of secret")
        self.device_id = device_id.strip()
        self._secret = bytes(secret)

    def headers(
        self,
        method: str,
        path: str,
        body: bytes,
        *,
        timestamp: int | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        stamp = int(time.time()) if timestamp is None else timestamp
        request_nonce = nonce or secrets.token_hex(16)
        digest = hashlib.sha256(body).hexdigest()
        canonical = "\n".join(
            (self.device_id, method.upper(), path, str(stamp), request_nonce, digest)
        ).encode("utf-8")
        signature = hmac.new(self._secret, canonical, hashlib.sha256).hexdigest()
        return {
            "X-Pangu-Device": self.device_id,
            "X-Pangu-Timestamp": str(stamp),
            "X-Pangu-Nonce": request_nonce,
            "X-Pangu-Body-SHA256": digest,
            "X-Pangu-Signature": signature,
        }


class PairedPhoneAdapter:
    """Secure REST bridge to an explicitly paired phone companion.

    This boundary never treats natural-language output as authorization. Consequential
    actions require an explicit `confirmed=True` from the policy/approval layer. Device
    unlocking is never bypassed: PANGU can only request the companion to present the
    platform biometric/device-credential UI and receive a yes/no result.
    """

    def __init__(
        self,
        base_url: str,
        signer: PairedDeviceSigner,
        policy: PairedDevicePolicy | None = None,
        opener: Callable[..., object] | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if not parsed.hostname or parsed.scheme not in {"http", "https"}:
            raise ValueError("invalid paired-device URL")
        self.policy = policy or PairedDevicePolicy()
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if self.policy.require_https and parsed.scheme != "https" and not local:
            raise ValueError("paired-device transport must use HTTPS")
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self._opener = opener or urllib.request.urlopen

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> object:
        body = (
            b""
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.signer.headers(method, path, body),
        }
        request = urllib.request.Request(
            self.base_url + path,
            data=body if method.upper() != "GET" else None,
            method=method.upper(),
            headers=headers,
        )
        response = self._opener(request, timeout=self.policy.timeout_seconds)
        with response:  # type: ignore[attr-defined]
            raw = response.read(self.policy.maximum_response_bytes + 1)  # type: ignore[attr-defined]
        if len(raw) > self.policy.maximum_response_bytes:
            raise RuntimeError("PAIRED_DEVICE_RESPONSE_TOO_LARGE")
        return json.loads(raw.decode("utf-8")) if raw else None

    def _result(
        self, operation: str, method: str, path: str, payload: dict[str, object] | None = None
    ) -> DeviceActionResult:
        try:
            data = self._request(method, path, payload)
        except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError):
            return DeviceActionResult(
                False, "Phone bridge unavailable.", normalized_error="PHONE_UNAVAILABLE"
            )
        return DeviceActionResult(True, operation, data)

    def health(self) -> bool:
        try:
            self._request("GET", "/v1/health")
            return True
        except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError):
            return False

    def capabilities(self) -> DeviceActionResult:
        return self._result("Read paired phone capabilities.", "GET", "/v1/capabilities")

    def notifications(self, *, limit: int = 20) -> DeviceActionResult:
        if not 1 <= limit <= 100:
            return DeviceActionResult(
                False, "Invalid notification limit.", normalized_error="INVALID_LIMIT"
            )
        return self._result(
            "Read paired phone notifications.",
            "GET",
            f"/v1/notifications?limit={limit}",
        )

    def request_device_authentication(self, reason: str) -> DeviceActionResult:
        clean = " ".join(reason.strip().split())
        if not clean or len(clean) > 240:
            return DeviceActionResult(
                False, "Invalid authentication reason.", normalized_error="INVALID_AUTH_REASON"
            )
        return self._result(
            "Requested phone biometric/device-credential authentication.",
            "POST",
            "/v1/authenticate",
            {"reason": clean},
        )

    def send_message(
        self,
        recipient: str,
        text: str,
        *,
        confirmed: bool = False,
    ) -> DeviceActionResult:
        if not confirmed:
            return DeviceActionResult(
                False,
                "Sending a message requires explicit confirmation.",
                confirmation_required=True,
                normalized_error="CONFIRMATION_REQUIRED",
            )
        if not recipient.strip() or not text.strip() or len(text) > 10_000:
            return DeviceActionResult(
                False, "Invalid message request.", normalized_error="INVALID_MESSAGE"
            )
        return self._result(
            "Message submitted to the paired phone.",
            "POST",
            "/v1/messages",
            {"recipient": recipient.strip(), "text": text},
        )

    def start_call(self, recipient: str, *, confirmed: bool = False) -> DeviceActionResult:
        if not confirmed:
            return DeviceActionResult(
                False,
                "Starting a call requires explicit confirmation.",
                confirmation_required=True,
                normalized_error="CONFIRMATION_REQUIRED",
            )
        if not recipient.strip() or len(recipient) > 200:
            return DeviceActionResult(
                False, "Invalid call target.", normalized_error="INVALID_CALL_TARGET"
            )
        return self._result(
            "Call request submitted to the paired phone.",
            "POST",
            "/v1/calls",
            {"recipient": recipient.strip()},
        )

    def answer_call(self, call_id: str, *, confirmed: bool = False) -> DeviceActionResult:
        if not confirmed:
            return DeviceActionResult(
                False,
                "Answering a call requires explicit confirmation or an owner-defined call rule.",
                confirmation_required=True,
                normalized_error="CONFIRMATION_REQUIRED",
            )
        if not call_id.strip() or len(call_id) > 200:
            return DeviceActionResult(False, "Invalid call id.", normalized_error="INVALID_CALL_ID")
        return self._result(
            "Incoming call answer requested.",
            "POST",
            f"/v1/calls/{quote(call_id, safe='')}/answer",
        )

    def end_call(self, call_id: str, *, confirmed: bool = True) -> DeviceActionResult:
        if not confirmed:
            return DeviceActionResult(
                False,
                "Ending this call requires confirmation.",
                confirmation_required=True,
                normalized_error="CONFIRMATION_REQUIRED",
            )
        if not call_id.strip() or len(call_id) > 200:
            return DeviceActionResult(False, "Invalid call id.", normalized_error="INVALID_CALL_ID")
        return self._result(
            "Call termination requested.",
            "POST",
            f"/v1/calls/{quote(call_id, safe='')}/hangup",
        )

    def call_state(self, call_id: str) -> DeviceActionResult:
        if not call_id.strip() or len(call_id) > 200:
            return DeviceActionResult(False, "Invalid call id.", normalized_error="INVALID_CALL_ID")
        return self._result(
            "Read call state.",
            "GET",
            f"/v1/calls/{quote(call_id, safe='')}",
        )

    def speak_on_call(
        self,
        call_id: str,
        text: str,
        *,
        assistant_disclosed: bool,
        confirmed: bool = False,
    ) -> DeviceActionResult:
        """Request assistant speech only when the companion advertises a supported media path.

        Android carrier-call media injection is not assumed. The companion must explicitly
        expose this endpoint/capability (for example, an app-owned VoIP/WebRTC call path).
        """
        if not assistant_disclosed:
            return DeviceActionResult(
                False,
                "PANGU must disclose that an automated assistant is speaking.",
                normalized_error="ASSISTANT_DISCLOSURE_REQUIRED",
            )
        if not confirmed:
            return DeviceActionResult(
                False,
                "Autonomous call speech requires an approved delegation session.",
                confirmation_required=True,
                normalized_error="CONFIRMATION_REQUIRED",
            )
        if not call_id.strip() or not text.strip() or len(text) > 4000:
            return DeviceActionResult(
                False, "Invalid call speech request.", normalized_error="INVALID_CALL_SPEECH"
            )
        return self._result(
            "Assistant speech submitted to the paired call media path.",
            "POST",
            f"/v1/calls/{quote(call_id, safe='')}/speak",
            {"text": text, "assistant_disclosed": True},
        )

    def push_context(
        self,
        context: dict[str, object],
        *,
        allowed_keys: frozenset[str],
    ) -> DeviceActionResult:
        filtered = {key: value for key, value in context.items() if key in allowed_keys}
        return self._result(
            "Filtered context synchronized.",
            "POST",
            "/v1/context",
            {"context": filtered},
        )
