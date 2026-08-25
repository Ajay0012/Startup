from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any

from .spatial_interaction import SemanticTarget


@dataclass(frozen=True)
class BrowserTargetSnapshot:
    browser: str
    window_title: str | None
    window_handle: int | None
    targets: tuple[SemanticTarget, ...]
    verification_state: str
    normalized_error: str | None = None


class ChromeSemanticTargetAdapter:
    """Read-only Chrome UIA adapter that exposes visible tabs as semantic targets.

    The adapter never clicks or closes tabs. It only converts the accessibility
    tree into normalized targets for the spatial proposal layer.
    """

    def __init__(self, *, title_contains: str = "Google Chrome", maximum_targets: int = 50) -> None:
        if not 1 <= maximum_targets <= 200:
            raise ValueError("maximum_targets must be between 1 and 200")
        self.title_contains = title_contains
        self.maximum_targets = maximum_targets

    @staticmethod
    def _safe(callable_value: Any, fallback: Any) -> Any:
        try:
            return callable_value()
        except Exception:  # noqa: BLE001 - UIA wrappers vary by Chrome/Windows version.
            return fallback

    @staticmethod
    def _normalized_rect(rect: Any, window_rect: Any) -> tuple[float, float, float, float] | None:
        try:
            window_width = max(1, int(window_rect.right) - int(window_rect.left))
            window_height = max(1, int(window_rect.bottom) - int(window_rect.top))
            left = (int(rect.left) - int(window_rect.left)) / window_width
            top = (int(rect.top) - int(window_rect.top)) / window_height
            width = (int(rect.right) - int(rect.left)) / window_width
            height = (int(rect.bottom) - int(rect.top)) / window_height
        except (AttributeError, TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return (
            min(1.0, max(0.0, left)),
            min(1.0, max(0.0, top)),
            min(1.0, max(0.0, width)),
            min(1.0, max(0.0, height)),
        )

    def discover(self) -> BrowserTargetSnapshot:
        try:
            pywinauto = import_module("pywinauto")
            desktop = pywinauto.Desktop(backend="uia")
            windows = list(desktop.windows())
        except (ImportError, ModuleNotFoundError):
            return BrowserTargetSnapshot(
                "chrome",
                None,
                None,
                (),
                "UNAVAILABLE",
                "UIA_BACKEND_UNAVAILABLE",
            )
        except Exception:  # noqa: BLE001 - normalize native UIA failures.
            return BrowserTargetSnapshot(
                "chrome",
                None,
                None,
                (),
                "UNVERIFIED",
                "BROWSER_WINDOW_ENUMERATION_FAILED",
            )

        chrome = None
        for window in windows:
            title = str(self._safe(window.window_text, "")).strip()
            info = getattr(window, "element_info", None)
            class_name = str(getattr(info, "class_name", "") or "")
            visible = bool(self._safe(window.is_visible, False))
            if visible and self.title_contains in title and class_name == "Chrome_WidgetWin_1":
                chrome = window
                break

        if chrome is None:
            return BrowserTargetSnapshot(
                "chrome",
                None,
                None,
                (),
                "VERIFIED",
                "CHROME_WINDOW_NOT_FOUND",
            )

        title = str(self._safe(chrome.window_text, "")).strip() or None
        handle = int(getattr(chrome, "handle", 0) or 0) or None
        window_rect = self._safe(chrome.rectangle, None)
        if window_rect is None:
            return BrowserTargetSnapshot(
                "chrome",
                title,
                handle,
                (),
                "UNVERIFIED",
                "CHROME_WINDOW_BOUNDS_UNAVAILABLE",
            )

        try:
            descendants = list(chrome.descendants())
        except Exception:  # noqa: BLE001
            return BrowserTargetSnapshot(
                "chrome",
                title,
                handle,
                (),
                "UNVERIFIED",
                "CHROME_ACCESSIBILITY_TREE_FAILED",
            )

        targets: list[SemanticTarget] = []
        for index, element in enumerate(descendants):
            if len(targets) >= self.maximum_targets:
                break
            name = str(self._safe(element.window_text, "")).strip()
            control_type = str(self._safe(element.friendly_class_name, ""))
            visible = bool(self._safe(element.is_visible, False))
            enabled = bool(self._safe(element.is_enabled, False))
            if not visible or not enabled or not name or control_type.casefold() != "tabitem":
                continue
            rect = self._safe(element.rectangle, None)
            normalized = self._normalized_rect(rect, window_rect)
            if normalized is None:
                continue
            x, y, width, height = normalized
            automation_id = str(
                getattr(getattr(element, "element_info", None), "automation_id", "") or ""
            )
            target_id = f"chrome:{handle or 0}:tab:{automation_id or index}:{name[:80]}"
            targets.append(
                SemanticTarget(
                    target_id=target_id,
                    kind="browser_tab",
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    closable=True,
                    destructive=False,
                    unsaved=False,
                    selection_count=1,
                )
            )

        return BrowserTargetSnapshot(
            "chrome",
            title,
            handle,
            tuple(targets),
            "VERIFIED",
            None,
        )
