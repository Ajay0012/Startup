from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlparse


class DeviceCapability(StrEnum):
    NOTIFICATION = "notification"
    MESSAGE = "message"
    CALL = "call"
    SMART_LIGHT = "smart_light"
    SMART_SWITCH = "smart_switch"
    SENSOR = "sensor"
    WEARABLE = "wearable"


@dataclass(frozen=True)
class TrustedDevice:
    device_id: str
    name: str
    capabilities: frozenset[DeviceCapability]
    trusted: bool = False


@dataclass(frozen=True)
class DeviceActionResult:
    success: bool
    message: str
    data: object = None
    confirmation_required: bool = False
    normalized_error: str | None = None


class DeviceAdapter(Protocol):
    def health(self) -> bool: ...


class SecureDeviceRegistry:
    def __init__(self) -> None:
        self._devices: dict[str, TrustedDevice] = {}
        self._adapters: dict[str, DeviceAdapter] = {}

    def register(self, device: TrustedDevice, adapter: DeviceAdapter) -> None:
        if device.device_id in self._devices:
            raise ValueError(f"duplicate device: {device.device_id}")
        self._devices[device.device_id] = device
        self._adapters[device.device_id] = adapter

    def set_trusted(self, device_id: str, trusted: bool) -> None:
        device = self._devices[device_id]
        self._devices[device_id] = TrustedDevice(
            device.device_id, device.name, device.capabilities, trusted
        )

    def devices(self) -> tuple[TrustedDevice, ...]:
        return tuple(self._devices.values())

    def adapter(self, device_id: str, capability: DeviceCapability) -> DeviceAdapter:
        device = self._devices[device_id]
        if capability not in device.capabilities:
            raise PermissionError(f"device does not expose {capability.value}")
        if not device.trusted:
            raise PermissionError("device is not trusted")
        return self._adapters[device_id]


class HomeAssistantRestAdapter:
    """Minimal Home Assistant REST bridge for low-risk smart-home domains.

    Tokens are supplied by configuration and never returned in results. Locks, alarms,
    covers, climate setpoints and other consequential domains are deliberately excluded
    from the default service allowlist.
    """

    _allowed_domains = frozenset({"light", "switch", "scene", "media_player"})
    _allowed_services = frozenset(
        {
            "turn_on",
            "turn_off",
            "toggle",
            "media_play_pause",
            "media_pause",
            "media_play",
            "volume_set",
        }
    )

    def __init__(self, base_url: str, token: str, timeout_seconds: float = 5.0) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid Home Assistant base URL")
        if not token.strip():
            raise ValueError("Home Assistant token is required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, payload: dict[str, object] | None = None) -> object:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read(1_000_000)
        return json.loads(raw.decode("utf-8")) if raw else None

    def health(self) -> bool:
        try:
            self._request("GET", "/api/")
            return True
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False

    def state(self, entity_id: str) -> DeviceActionResult:
        if "." not in entity_id or len(entity_id) > 200:
            return DeviceActionResult(
                False, "Invalid entity id.", normalized_error="INVALID_ENTITY"
            )
        try:
            data = self._request("GET", f"/api/states/{entity_id}")
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            return DeviceActionResult(
                False, str(error), normalized_error="HOME_ASSISTANT_UNAVAILABLE"
            )
        return DeviceActionResult(True, f"Read {entity_id} state.", data)

    def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        data: dict[str, object] | None = None,
    ) -> DeviceActionResult:
        domain = domain.casefold()
        service = service.casefold()
        if domain not in self._allowed_domains or service not in self._allowed_services:
            return DeviceActionResult(
                False,
                "That smart-home operation requires a dedicated approval policy.",
                confirmation_required=True,
                normalized_error="SMART_HOME_OPERATION_NOT_ALLOWED",
            )
        payload = {"entity_id": entity_id, **(data or {})}
        try:
            result = self._request("POST", f"/api/services/{domain}/{service}", payload)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            return DeviceActionResult(
                False, str(error), normalized_error="HOME_ASSISTANT_UNAVAILABLE"
            )
        return DeviceActionResult(True, f"Requested {domain}.{service} for {entity_id}.", result)
