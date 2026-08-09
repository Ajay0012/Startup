from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol


@dataclass(frozen=True)
class ScreenRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


@dataclass(frozen=True)
class UIElement:
    element_id: str
    name: str
    control_type: str
    automation_id: str
    class_name: str
    bounds: ScreenRect
    enabled: bool
    visible: bool
    focusable: bool
    is_password: bool
    window_handle: int


@dataclass(frozen=True)
class ScreenSnapshot:
    backend: str
    verification_state: str
    active_window_title: str | None
    active_window_handle: int | None
    elements: tuple[UIElement, ...]
    truncated: bool = False
    normalized_error: str | None = None


class ScreenPerceptionAdapter(Protocol):
    def snapshot(self) -> ScreenSnapshot: ...


class WindowsUIAutomationAdapter:
    """Read-only Windows accessibility snapshot; no clicks or keystrokes occur here."""

    def __init__(self, maximum_elements: int = 500) -> None:
        if not 10 <= maximum_elements <= 5000:
            raise ValueError("maximum_elements must be between 10 and 5000")
        self.maximum_elements = maximum_elements

    @staticmethod
    def _rect(value: object) -> ScreenRect:
        try:
            rect = value
            return ScreenRect(
                int(getattr(rect, "left")),
                int(getattr(rect, "top")),
                int(getattr(rect, "right")),
                int(getattr(rect, "bottom")),
            )
        except (AttributeError, TypeError, ValueError):
            return ScreenRect(0, 0, 0, 0)

    @staticmethod
    def _safe(callable_value: Any, fallback: Any) -> Any:
        try:
            return callable_value()
        except Exception:  # noqa: BLE001 - third-party accessibility wrappers vary by app.
            return fallback

    def snapshot(self) -> ScreenSnapshot:
        try:
            pywinauto = import_module("pywinauto")
            desktop = pywinauto.Desktop(backend="uia")
            windows = list(desktop.windows())
        except (ImportError, ModuleNotFoundError):
            return ScreenSnapshot(
                "windows-uia", "UNAVAILABLE", None, None, (), normalized_error="UIA_BACKEND_UNAVAILABLE"
            )
        except Exception:  # noqa: BLE001 - native COM/UIA failures are normalized.
            return ScreenSnapshot(
                "windows-uia", "UNVERIFIED", None, None, (), normalized_error="UIA_SNAPSHOT_FAILED"
            )

        active: Any | None = None
        for window in windows:
            if bool(self._safe(window.is_active, False)):
                active = window
                break
        if active is None:
            active = next(
                (window for window in windows if bool(self._safe(window.is_visible, False))),
                None,
            )
        if active is None:
            return ScreenSnapshot("windows-uia", "VERIFIED", None, None, ())

        title = str(self._safe(active.window_text, "")) or None
        handle = int(getattr(active, "handle", 0) or 0) or None
        try:
            descendants = list(active.descendants())
        except Exception:  # noqa: BLE001
            descendants = []
        truncated = len(descendants) > self.maximum_elements
        elements: list[UIElement] = []
        for index, control in enumerate(descendants[: self.maximum_elements]):
            info = getattr(control, "element_info", None)
            name = str(self._safe(control.window_text, ""))
            control_type = str(self._safe(control.friendly_class_name, "Unknown"))
            automation_id = str(getattr(info, "automation_id", "") or "")
            class_name = str(getattr(info, "class_name", "") or "")
            rectangle = self._safe(control.rectangle, None)
            is_password = bool(getattr(info, "is_password", False))
            enabled = bool(self._safe(control.is_enabled, False))
            visible = bool(self._safe(control.is_visible, False))
            focusable = bool(getattr(info, "is_keyboard_focusable", False))
            signature = f"{handle}:{automation_id}:{control_type}:{name}:{index}"
            elements.append(
                UIElement(
                    signature,
                    name[:512],
                    control_type[:128],
                    automation_id[:512],
                    class_name[:256],
                    self._rect(rectangle),
                    enabled,
                    visible,
                    focusable,
                    is_password,
                    int(handle or 0),
                )
            )
        return ScreenSnapshot(
            "windows-uia",
            "VERIFIED",
            title,
            handle,
            tuple(elements),
            truncated,
        )


class ScreenPerceptionRuntime:
    """One bounded screen/accessibility perception boundary for PANGU."""

    def __init__(self, adapter: ScreenPerceptionAdapter | None = None) -> None:
        self.adapter = adapter or WindowsUIAutomationAdapter()
        self.last_snapshot: ScreenSnapshot | None = None

    def capture(self) -> ScreenSnapshot:
        snapshot = self.adapter.snapshot()
        self.last_snapshot = snapshot
        return snapshot

    def find(
        self,
        query: str,
        *,
        control_type: str | None = None,
        limit: int = 12,
    ) -> tuple[UIElement, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        snapshot = self.capture()
        key = query.casefold().strip()
        matches = [
            item
            for item in snapshot.elements
            if (not key or key in item.name.casefold() or key in item.automation_id.casefold())
            and (control_type is None or item.control_type.casefold() == control_type.casefold())
            and item.visible
        ]
        return tuple(matches[:limit])
