"""Typed, verified system audio and display brightness control.

Native integrations are deliberately optional.  This keeps the core portable and
means an unavailable Windows binding is reported as UNSUPPORTED, never faked.
"""

from __future__ import annotations

import platform
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import import_module
from typing import Any, Protocol, TypeVar

from .capabilities import CapabilityCatalog
from .contracts import CommandEnvelope, Risk, Status, ToolRequest, ToolResult
from .database import DatabaseService
from .permissions import PermissionStore
from .security import SafetyGateway

T = TypeVar("T", int, bool)


class SystemErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    DENIED = "DENIED"
    NATIVE_FAILURE = "NATIVE_FAILURE"
    UNSUPPORTED = "UNSUPPORTED"
    POSTCONDITION_TIMEOUT = "POSTCONDITION_TIMEOUT"
    INVALID = "INVALID"
    AUDIO_ADAPTER_UNAVAILABLE = "AUDIO_ADAPTER_UNAVAILABLE"
    NO_ACTIVE_AUDIO_ENDPOINT = "NO_ACTIVE_AUDIO_ENDPOINT"
    BRIGHTNESS_ADAPTER_UNAVAILABLE = "BRIGHTNESS_ADAPTER_UNAVAILABLE"
    NO_COMPATIBLE_DISPLAY = "NO_COMPATIBLE_DISPLAY"


class SystemVerification(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    UNSUPPORTED = "UNSUPPORTED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class Display:
    selector: str
    name: str
    brightness: int | None


@dataclass(frozen=True)
class SystemControlDiagnostics:
    platform: str
    audio_available: bool
    audio_backend: str | None
    audio_unavailable_reason: SystemErrorCode | None
    brightness_available: bool
    brightness_backend: str | None
    brightness_unavailable_reason: SystemErrorCode | None

    def public(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "audio_available": self.audio_available,
            "audio_backend": self.audio_backend,
            "audio_unavailable_reason": self.audio_unavailable_reason.value
            if self.audio_unavailable_reason
            else None,
            "brightness_available": self.brightness_available,
            "brightness_backend": self.brightness_backend,
            "brightness_unavailable_reason": self.brightness_unavailable_reason.value
            if self.brightness_unavailable_reason
            else None,
        }


@dataclass(frozen=True)
class SystemControlResult:
    operation: str
    requested_value: int | bool | None = None
    previous_value: int | bool | None = None
    observed_value: int | bool | None = None
    requested_outcome: str = "read"
    observed_outcome: str = "unknown"
    verification_state: SystemVerification = SystemVerification.UNVERIFIED
    confidence: float = 0.0
    adapter_name: str = "unknown"
    evidence: dict[str, object] = field(default_factory=dict)
    remaining_uncertainty: str | None = None
    retryable: bool = False
    normalized_error: SystemErrorCode | None = None

    def public(self) -> dict[str, object]:
        value = self.__dict__.copy()
        value["verification_state"] = self.verification_state.value
        value["normalized_error"] = self.normalized_error.value if self.normalized_error else None
        return value


class SystemControlAdapter(Protocol):
    name: str

    def get_volume(self) -> int: ...
    def set_volume(self, value: int) -> None: ...
    def get_mute(self) -> bool: ...
    def set_mute(self, value: bool) -> None: ...
    def displays(self) -> list[Display]: ...
    def set_brightness(self, selector: str, value: int) -> None: ...
    def diagnostics(self) -> SystemControlDiagnostics: ...


class SystemControlFailure(RuntimeError):
    def __init__(self, code: SystemErrorCode, detail: str = "") -> None:
        self.code = code
        super().__init__(detail or code.value)


class FakeSystemControlAdapter:
    name = "fake-system-control"

    def __init__(
        self, volume: int = 50, muted: bool = False, displays: list[Display] | None = None
    ) -> None:
        self.volume, self.muted = volume, muted
        self._displays = (
            displays if displays is not None else [Display("display-1", "Primary display", 50)]
        )
        self.fail: SystemErrorCode | None = None
        self.audio_fail: SystemErrorCode | None = None
        self.brightness_fail: SystemErrorCode | None = None
        self.apply_changes = True

    def _check(self) -> None:
        if self.fail:
            raise SystemControlFailure(self.fail)

    def get_volume(self) -> int:
        self._check()
        if self.audio_fail:
            raise SystemControlFailure(self.audio_fail)
        return self.volume

    def set_volume(self, value: int) -> None:
        self._check()
        if self.audio_fail:
            raise SystemControlFailure(self.audio_fail)
        if self.apply_changes:
            self.volume = value

    def get_mute(self) -> bool:
        self._check()
        if self.audio_fail:
            raise SystemControlFailure(self.audio_fail)
        return self.muted

    def set_mute(self, value: bool) -> None:
        self._check()
        if self.audio_fail:
            raise SystemControlFailure(self.audio_fail)
        if self.apply_changes:
            self.muted = value

    def displays(self) -> list[Display]:
        self._check()
        if self.brightness_fail:
            raise SystemControlFailure(self.brightness_fail)
        return list(self._displays)

    def set_brightness(self, selector: str, value: int) -> None:
        self._check()
        if self.brightness_fail:
            raise SystemControlFailure(self.brightness_fail)
        matches = [x for x in self._displays if x.selector == selector]
        if not matches:
            raise SystemControlFailure(SystemErrorCode.NOT_FOUND)
        if self.apply_changes:
            self._displays = [
                Display(x.selector, x.name, value if x.selector == selector else x.brightness)
                for x in self._displays
            ]

    def diagnostics(self) -> SystemControlDiagnostics:
        return SystemControlDiagnostics(
            "fake",
            self.audio_fail is None,
            "fake" if self.audio_fail is None else None,
            self.audio_fail,
            self.brightness_fail is None,
            "fake" if self.brightness_fail is None else None,
            self.brightness_fail,
        )


class WindowsSystemControlAdapter:
    """Optional bindings; imports and native calls stay within this adapter."""

    name = "windows-core-audio-and-brightness"

    def _audio(self) -> Any:
        if platform.system() != "Windows":
            raise SystemControlFailure(SystemErrorCode.AUDIO_ADAPTER_UNAVAILABLE)
        try:
            audio_utilities = import_module("pycaw.pycaw").AudioUtilities
            device = audio_utilities.GetSpeakers()
            if device is None:
                raise SystemControlFailure(SystemErrorCode.NO_ACTIVE_AUDIO_ENDPOINT)
            return device.EndpointVolume
        except SystemControlFailure:
            raise
        except (ImportError, ModuleNotFoundError) as error:
            raise SystemControlFailure(SystemErrorCode.AUDIO_ADAPTER_UNAVAILABLE) from error
        except Exception as error:
            raise SystemControlFailure(SystemErrorCode.NO_ACTIVE_AUDIO_ENDPOINT) from error

    def get_volume(self) -> int:
        return round(float(self._audio().GetMasterVolumeLevelScalar()) * 100)

    def set_volume(self, value: int) -> None:
        self._audio().SetMasterVolumeLevelScalar(value / 100, None)

    def get_mute(self) -> bool:
        return bool(self._audio().GetMute())

    def set_mute(self, value: bool) -> None:
        self._audio().SetMute(value, None)

    def _brightness(self) -> Any:
        if platform.system() != "Windows":
            raise SystemControlFailure(SystemErrorCode.BRIGHTNESS_ADAPTER_UNAVAILABLE)
        try:
            return import_module("screen_brightness_control")
        except (ImportError, ModuleNotFoundError) as error:
            raise SystemControlFailure(SystemErrorCode.BRIGHTNESS_ADAPTER_UNAVAILABLE) from error
        except Exception as error:
            raise SystemControlFailure(SystemErrorCode.BRIGHTNESS_ADAPTER_UNAVAILABLE) from error

    def displays(self) -> list[Display]:
        sbc = self._brightness()
        try:
            info = sbc.list_monitors_info()
            result: list[Display] = []
            for i, item in enumerate(info):
                value = sbc.get_brightness(display=i)
                result.append(
                    Display(
                        f"display-{i + 1}",
                        str(item.get("name", f"Display {i + 1}")),
                        int(value[0] if isinstance(value, list) else value),
                    )
                )
            return result
        except Exception as error:
            raise SystemControlFailure(SystemErrorCode.NO_COMPATIBLE_DISPLAY) from error

    def set_brightness(self, selector: str, value: int) -> None:
        index = int(selector.removeprefix("display-")) - 1
        try:
            self._brightness().set_brightness(value, display=index)
        except Exception as error:
            raise SystemControlFailure(SystemErrorCode.NATIVE_FAILURE) from error

    def diagnostics(self) -> SystemControlDiagnostics:
        audio_error: SystemErrorCode | None = None
        brightness_error: SystemErrorCode | None = None
        try:
            self._audio()
        except SystemControlFailure as error:
            audio_error = error.code
        try:
            self.displays()
        except SystemControlFailure as error:
            brightness_error = error.code
        return SystemControlDiagnostics(
            platform.system(),
            audio_error is None,
            "pycaw" if audio_error is None else None,
            audio_error,
            brightness_error is None,
            "screen_brightness_control" if brightness_error is None else None,
            brightness_error,
        )


class SystemControlRuntime:
    def __init__(
        self,
        adapter: SystemControlAdapter,
        catalog: CapabilityCatalog,
        permissions: PermissionStore,
        safety: SafetyGateway,
        database: DatabaseService,
    ) -> None:
        self.adapter, self.catalog, self.permissions, self.safety, self.database = (
            adapter,
            catalog,
            permissions,
            safety,
            database,
        )

    def _permit(self, capability: str, operation: str, actor: str) -> SystemControlResult | None:
        try:
            self.catalog.resolve("system.control", operation)
        except LookupError:
            return self._error(operation, SystemErrorCode.UNSUPPORTED)
        request = ToolRequest("system.control", operation, {}, actor)
        if self.safety.classify(request) == Risk.PROHIBITED or not self.permissions.allows(
            actor, capability
        ):
            return self._error(operation, SystemErrorCode.DENIED, SystemVerification.DENIED)
        return None

    def _error(
        self,
        operation: str,
        error: SystemErrorCode,
        state: SystemVerification | None = None,
        evidence: dict[str, object] | None = None,
    ) -> SystemControlResult:
        return SystemControlResult(
            operation,
            verification_state=state
            or (
                SystemVerification.AMBIGUOUS
                if error == SystemErrorCode.AMBIGUOUS
                else SystemVerification.UNSUPPORTED
                if error
                in {
                    SystemErrorCode.UNSUPPORTED,
                    SystemErrorCode.AUDIO_ADAPTER_UNAVAILABLE,
                    SystemErrorCode.NO_ACTIVE_AUDIO_ENDPOINT,
                    SystemErrorCode.BRIGHTNESS_ADAPTER_UNAVAILABLE,
                    SystemErrorCode.NO_COMPATIBLE_DISPLAY,
                }
                else SystemVerification.FAILED
            ),
            adapter_name=self.adapter.name,
            evidence=evidence or {},
            normalized_error=error,
            retryable=error
            in {SystemErrorCode.NATIVE_FAILURE, SystemErrorCode.POSTCONDITION_TIMEOUT},
        )

    def _audit(self, result: SystemControlResult, actor: str) -> SystemControlResult:
        command = CommandEnvelope(f"system:{result.operation}", "system-control", user_id=actor)
        tool = ToolResult(
            command.command_id,
            Status.VERIFIED
            if result.verification_state == SystemVerification.VERIFIED
            else Status.DENIED
            if result.verification_state == SystemVerification.DENIED
            else Status.UNVERIFIED,
            result.operation,
            result.public(),
            {
                "capability": self._capability(result.operation),
                "adapter": result.adapter_name,
                "normalized_error": result.normalized_error.value
                if result.normalized_error
                else None,
            },
        )
        self.database.record(command, tool)
        return result

    def _capability(self, operation: str) -> str:
        return (
            "system.brightness.write"
            if "brightness" in operation and operation != "get_brightness"
            else "system.brightness.read"
            if operation == "get_brightness"
            else "system.audio.write"
            if operation not in {"get_volume", "get_mute_state"}
            else "system.audio.read"
        )

    def _value(self, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SystemControlFailure(SystemErrorCode.INVALID)
        return max(0, min(100, value))

    def _observe(self, read: Callable[[], T], matches: Callable[[T], bool]) -> T | None:
        deadline = time.monotonic() + 0.5
        observed: T | None = None
        while time.monotonic() < deadline:
            observed = read()
            if matches(observed):
                return observed
            time.sleep(0.02)
        return observed

    def audio(
        self, operation: str, value: int | bool | None = None, actor: str = "default"
    ) -> SystemControlResult:
        denied = self._permit(self._capability(operation), operation, actor)
        if denied:
            return self._audit(denied, actor)
        try:
            if operation == "get_volume":
                result = SystemControlResult(
                    operation,
                    observed_value=self.adapter.get_volume(),
                    observed_outcome="read",
                    verification_state=SystemVerification.VERIFIED,
                    confidence=1,
                    adapter_name=self.adapter.name,
                )
            elif operation == "get_mute_state":
                result = SystemControlResult(
                    operation,
                    observed_value=self.adapter.get_mute(),
                    observed_outcome="read",
                    verification_state=SystemVerification.VERIFIED,
                    confidence=1,
                    adapter_name=self.adapter.name,
                )
            elif operation in {"set_volume", "increase_volume", "decrease_volume"}:
                old = self.adapter.get_volume()
                requested = self._value(
                    value
                    if operation == "set_volume"
                    else old
                    + (
                        self._value(5 if value is None else value)
                        * (1 if operation == "increase_volume" else -1)
                    )
                )
                self.adapter.set_volume(requested)
                observed = self._observe(
                    self.adapter.get_volume, lambda item: abs(item - requested) <= 1
                )
                ok = observed is not None and abs(observed - requested) <= 1
                result = SystemControlResult(
                    operation,
                    requested,
                    old,
                    observed,
                    "set volume",
                    "volume observed",
                    SystemVerification.VERIFIED if ok else SystemVerification.UNVERIFIED,
                    1 if ok else 0.5,
                    self.adapter.name,
                    remaining_uncertainty=None
                    if ok
                    else "endpoint did not report requested volume",
                    retryable=not ok,
                    normalized_error=None if ok else SystemErrorCode.POSTCONDITION_TIMEOUT,
                )
            else:
                old = self.adapter.get_mute()
                requested = operation == "mute" if operation in {"mute", "unmute"} else not old
                self.adapter.set_mute(requested)
                observed = self._observe(self.adapter.get_mute, lambda item: item == requested)
                ok = observed is not None and observed == requested
                result = SystemControlResult(
                    operation,
                    requested,
                    old,
                    observed,
                    "set mute",
                    "mute observed",
                    SystemVerification.VERIFIED if ok else SystemVerification.UNVERIFIED,
                    1 if ok else 0.5,
                    self.adapter.name,
                    remaining_uncertainty=None
                    if ok
                    else "endpoint did not report requested mute state",
                    retryable=not ok,
                    normalized_error=None if ok else SystemErrorCode.POSTCONDITION_TIMEOUT,
                )
        except SystemControlFailure as error:
            result = self._error(operation, error.code)
        return self._audit(result, actor)

    def brightness(
        self,
        operation: str,
        value: int | None = None,
        selector: str | None = None,
        actor: str = "default",
    ) -> SystemControlResult:
        denied = self._permit(self._capability(operation), operation, actor)
        if denied:
            return self._audit(denied, actor)
        try:
            displays = self.adapter.displays()
            if operation == "get_brightness" and selector is None:
                return self._audit(
                    SystemControlResult(
                        operation,
                        observed_outcome="listed",
                        verification_state=SystemVerification.VERIFIED,
                        confidence=1,
                        adapter_name=self.adapter.name,
                        evidence={"displays": [x.__dict__ for x in displays]},
                    ),
                    actor,
                )
            selected = [x for x in displays if x.selector == selector] if selector else displays
            if not selected:
                result = self._error(operation, SystemErrorCode.NOT_FOUND)
            elif len(selected) != 1:
                result = self._error(
                    operation,
                    SystemErrorCode.AMBIGUOUS,
                    evidence={
                        "options": [{"selector": x.selector, "name": x.name} for x in selected]
                    },
                )
            elif operation == "get_brightness":
                result = SystemControlResult(
                    operation,
                    observed_value=selected[0].brightness,
                    observed_outcome="read",
                    verification_state=SystemVerification.VERIFIED,
                    confidence=1,
                    adapter_name=self.adapter.name,
                )
            else:
                old = selected[0].brightness
                assert old is not None
                requested = self._value(
                    value
                    if operation == "set_brightness"
                    else old
                    + self._value(5 if value is None else value)
                    * (1 if operation == "increase_brightness" else -1)
                )
                self.adapter.set_brightness(selected[0].selector, requested)
                deadline = time.monotonic() + 0.5
                observed = None
                while time.monotonic() < deadline:
                    current = [
                        x for x in self.adapter.displays() if x.selector == selected[0].selector
                    ]
                    observed = current[0].brightness if current else None
                    if observed == requested:
                        break
                    time.sleep(0.02)
                ok = observed == requested
                result = SystemControlResult(
                    operation,
                    requested,
                    old,
                    observed,
                    "set brightness",
                    "brightness observed",
                    SystemVerification.VERIFIED if ok else SystemVerification.UNVERIFIED,
                    1 if ok else 0.5,
                    self.adapter.name,
                    remaining_uncertainty=None
                    if ok
                    else "brightness did not reach requested value",
                    retryable=not ok,
                    normalized_error=None if ok else SystemErrorCode.POSTCONDITION_TIMEOUT,
                )
        except SystemControlFailure as error:
            result = self._error(operation, error.code)
        return self._audit(result, actor)
