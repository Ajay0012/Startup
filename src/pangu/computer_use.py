from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from importlib import import_module
from typing import Any, Protocol

from .screen_perception import ScreenPerceptionRuntime, UIElement, WindowsUIAutomationAdapter


class ComputerActionKind(StrEnum):
    FOCUS = "focus"
    INVOKE = "invoke"
    SET_TEXT = "set_text"
    SCROLL = "scroll"


class ComputerUseState(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    DENIED = "DENIED"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ComputerTarget:
    name: str = ""
    automation_id: str = ""
    control_type: str | None = None
    window_handle: int | None = None


@dataclass(frozen=True)
class ComputerActionRequest:
    action: ComputerActionKind
    target: ComputerTarget
    text: str | None = None
    scroll_amount: int = 0


@dataclass(frozen=True)
class ComputerActionResult:
    action: ComputerActionKind
    state: ComputerUseState
    message: str
    matched_element: UIElement | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    normalized_error: str | None = None


class ComputerActionAdapter(Protocol):
    def focus(self, element: UIElement) -> bool: ...
    def invoke(self, element: UIElement) -> bool: ...
    def set_text(self, element: UIElement, text: str) -> bool: ...
    def scroll(self, element: UIElement, amount: int) -> bool: ...


class VisualFallbackAdapter(Protocol):
    def execute(self, request: ComputerActionRequest) -> ComputerActionResult: ...


class WindowsUIAutomationActionAdapter:
    """Native UIA action adapter. Targets are resolved structurally before every action."""

    @staticmethod
    def _desktop() -> Any:
        pywinauto = import_module("pywinauto")
        return pywinauto.Desktop(backend="uia")

    def _resolve_wrapper(self, element: UIElement) -> Any | None:
        try:
            desktop = self._desktop()
            window = desktop.window(handle=element.window_handle)
            kwargs: dict[str, object] = {}
            if element.automation_id:
                kwargs["auto_id"] = element.automation_id
            if element.name:
                kwargs["title"] = element.name
            if element.control_type and element.control_type != "Unknown":
                kwargs["control_type"] = element.control_type
            candidate = window.child_window(**kwargs) if kwargs else window
            return candidate.wrapper_object()
        except (ImportError, ModuleNotFoundError):
            return None
        except Exception:  # noqa: BLE001 - native UIA errors are normalized by caller.
            return None

    def focus(self, element: UIElement) -> bool:
        wrapper = self._resolve_wrapper(element)
        if wrapper is None:
            return False
        try:
            wrapper.set_focus()
            return True
        except Exception:  # noqa: BLE001
            return False

    def invoke(self, element: UIElement) -> bool:
        wrapper = self._resolve_wrapper(element)
        if wrapper is None:
            return False
        try:
            if hasattr(wrapper, "invoke"):
                wrapper.invoke()
            elif hasattr(wrapper, "click"):
                wrapper.click()
            else:
                return False
            return True
        except Exception:  # noqa: BLE001
            return False

    def set_text(self, element: UIElement, text: str) -> bool:
        wrapper = self._resolve_wrapper(element)
        if wrapper is None:
            return False
        try:
            if hasattr(wrapper, "set_edit_text"):
                wrapper.set_edit_text(text)
            elif hasattr(wrapper, "set_value"):
                wrapper.set_value(text)
            else:
                return False
            return True
        except Exception:  # noqa: BLE001
            return False

    def scroll(self, element: UIElement, amount: int) -> bool:
        wrapper = self._resolve_wrapper(element)
        if wrapper is None:
            return False
        try:
            if not hasattr(wrapper, "scroll"):
                return False
            direction = "down" if amount > 0 else "up"
            wrapper.scroll(direction, abs(amount))
            return True
        except Exception:  # noqa: BLE001
            return False


class ComputerUseRuntime:
    """Typed accessibility-first computer use with a guarded visual fallback."""

    _sensitive_terms = frozenset(
        {
            "buy",
            "purchase",
            "pay",
            "send",
            "submit",
            "confirm",
            "delete",
            "remove",
            "uninstall",
            "install",
            "allow",
            "grant",
            "authorize",
            "reset",
            "format",
            "shutdown",
            "restart",
            "sign out",
            "log out",
            "transfer",
            "withdraw",
        }
    )

    def __init__(
        self,
        perception: ScreenPerceptionRuntime | None = None,
        adapter: ComputerActionAdapter | None = None,
        visual_fallback: VisualFallbackAdapter | None = None,
    ) -> None:
        self.perception = perception or ScreenPerceptionRuntime(WindowsUIAutomationAdapter())
        self.adapter = adapter or WindowsUIAutomationActionAdapter()
        self.visual_fallback = visual_fallback

    @classmethod
    def _sensitive(cls, element: UIElement) -> bool:
        text = f"{element.name} {element.automation_id}".casefold()
        return any(term in text for term in cls._sensitive_terms)

    def _resolve(self, target: ComputerTarget) -> tuple[UIElement | None, str | None]:
        snapshot = self.perception.capture()
        if snapshot.verification_state != "VERIFIED":
            return None, snapshot.normalized_error or "SCREEN_PERCEPTION_UNAVAILABLE"
        candidates = [item for item in snapshot.elements if item.visible and item.enabled]
        if target.window_handle is not None:
            candidates = [item for item in candidates if item.window_handle == target.window_handle]
        if target.automation_id:
            candidates = [item for item in candidates if item.automation_id == target.automation_id]
        if target.name:
            key = target.name.casefold().strip()
            exact = [item for item in candidates if item.name.casefold().strip() == key]
            candidates = exact or [item for item in candidates if key in item.name.casefold()]
        if target.control_type:
            candidates = [
                item
                for item in candidates
                if item.control_type.casefold() == target.control_type.casefold()
            ]
        if not candidates:
            return None, "TARGET_NOT_FOUND"
        if len(candidates) != 1:
            return None, "TARGET_AMBIGUOUS"
        return candidates[0], None

    def execute(self, request: ComputerActionRequest) -> ComputerActionResult:
        target, error = self._resolve(request.target)
        if target is None:
            if error == "TARGET_NOT_FOUND" and self.visual_fallback is not None:
                return self.visual_fallback.execute(request)
            return ComputerActionResult(
                request.action,
                ComputerUseState.DENIED
                if error == "TARGET_AMBIGUOUS"
                else ComputerUseState.UNSUPPORTED,
                "Computer target could not be resolved safely.",
                normalized_error=error,
            )
        if target.is_password:
            return ComputerActionResult(
                request.action,
                ComputerUseState.DENIED,
                "Password fields are not controlled by PANGU computer-use.",
                target,
                normalized_error="PASSWORD_FIELD_BLOCKED",
            )
        if request.action != ComputerActionKind.FOCUS and self._sensitive(target):
            return ComputerActionResult(
                request.action,
                ComputerUseState.DENIED,
                "This control may commit a consequential action and requires a dedicated approval flow.",
                target,
                normalized_error="CONSEQUENTIAL_CONTROL_REQUIRES_APPROVAL",
            )
        if request.action == ComputerActionKind.SET_TEXT:
            if request.text is None or len(request.text) > 20_000:
                return ComputerActionResult(
                    request.action,
                    ComputerUseState.DENIED,
                    "Text input is missing or exceeds the bounded input size.",
                    target,
                    normalized_error="INVALID_TEXT_INPUT",
                )
            succeeded = self.adapter.set_text(target, request.text)
        elif request.action == ComputerActionKind.INVOKE:
            succeeded = self.adapter.invoke(target)
        elif request.action == ComputerActionKind.FOCUS:
            succeeded = self.adapter.focus(target)
        elif request.action == ComputerActionKind.SCROLL:
            if request.scroll_amount == 0 or abs(request.scroll_amount) > 20:
                return ComputerActionResult(
                    request.action,
                    ComputerUseState.DENIED,
                    "Scroll amount is outside the bounded range.",
                    target,
                    normalized_error="INVALID_SCROLL_AMOUNT",
                )
            succeeded = self.adapter.scroll(target, request.scroll_amount)
        else:
            return ComputerActionResult(
                request.action,
                ComputerUseState.UNSUPPORTED,
                "Unsupported computer action.",
                target,
            )

        if not succeeded:
            return ComputerActionResult(
                request.action,
                ComputerUseState.FAILED,
                "The native UI Automation action failed.",
                target,
                normalized_error="UIA_ACTION_FAILED",
            )

        after = self.perception.capture()
        healthy = after.verification_state == "VERIFIED"
        state = (
            ComputerUseState.VERIFIED
            if request.action == ComputerActionKind.FOCUS and healthy
            else ComputerUseState.UNVERIFIED
        )
        return ComputerActionResult(
            request.action,
            state,
            "Computer action executed through Windows UI Automation.",
            target,
            {
                "active_window_handle": after.active_window_handle,
                "active_window_title": after.active_window_title,
                "screen_snapshot_verified": healthy,
            },
            None if healthy else after.normalized_error or "POSTCONDITION_UNVERIFIED",
        )
