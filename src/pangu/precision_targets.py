from __future__ import annotations

from dataclasses import dataclass

from .browser_targets import ChromeSemanticTargetAdapter
from .screen_perception import ScreenPerceptionRuntime, WindowsUIAutomationAdapter
from .spatial_interaction import SemanticTarget


@dataclass(frozen=True)
class PrecisionTargetSnapshot:
    targets: tuple[SemanticTarget, ...]
    verification_state: str
    active_window_title: str | None
    active_window_handle: int | None
    truncated: bool = False


class PrecisionSemanticTargetAdapter:
    """Expose actionable UIA elements as bounded semantic targets for hand pointing.

    This adapter is read-only. It intentionally excludes password fields and giant
    container controls so hand pointing can lock onto buttons/tabs/links/inputs rather
    than arbitrary panes. Coordinates use the Windows virtual desktop so they align
    with the HUD and browser targets.
    """

    _ACTIONABLE = {
        "button",
        "hyperlink",
        "menuitem",
        "tabitem",
        "edit",
        "checkbox",
        "radiobutton",
        "combobox",
        "listitem",
        "treeitem",
        "slider",
    }

    def __init__(self, maximum_elements: int = 900, maximum_targets: int = 180) -> None:
        if not 1 <= maximum_targets <= 500:
            raise ValueError("maximum_targets must be between 1 and 500")
        self.runtime = ScreenPerceptionRuntime(WindowsUIAutomationAdapter(maximum_elements))
        self.maximum_targets = maximum_targets

    @staticmethod
    def _target_kind(control_type: str) -> str:
        key = control_type.casefold()
        if key == "tabitem":
            return "browser_tab"
        if key in {"button", "hyperlink", "menuitem"}:
            return "ui_action"
        if key in {"edit", "combobox", "slider"}:
            return "ui_input"
        return "ui_control"

    def discover(self) -> PrecisionTargetSnapshot:
        snapshot = self.runtime.capture()
        reference = ChromeSemanticTargetAdapter._virtual_screen_rect()
        if reference is None:
            return PrecisionTargetSnapshot(
                (),
                "UNVERIFIED",
                snapshot.active_window_title,
                snapshot.active_window_handle,
                snapshot.truncated,
            )

        targets: list[SemanticTarget] = []
        for element in snapshot.elements:
            if len(targets) >= self.maximum_targets:
                break
            control_type = element.control_type.casefold()
            if (
                control_type not in self._ACTIONABLE
                or not element.visible
                or not element.enabled
                or element.is_password
                or element.bounds.width < 5
                or element.bounds.height < 5
            ):
                continue

            normalized = ChromeSemanticTargetAdapter._normalized_rect(element.bounds, reference)
            if normalized is None:
                continue
            x, y, width, height = normalized
            # Ignore large layout/container-like controls even if a framework labels
            # them as actionable. Precision pointing should prefer real hit targets.
            if width * height > 0.18 or width > 0.75 or height > 0.55:
                continue

            name = " ".join(element.name.split())[:80]
            if not name and not element.automation_id and not element.focusable:
                continue
            identity = element.automation_id or name or element.element_id
            targets.append(
                SemanticTarget(
                    target_id=(
                        f"uia:{element.window_handle}:{control_type}:"
                        f"{identity[:90]}:{len(targets)}"
                    ),
                    kind=self._target_kind(element.control_type),
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    closable=control_type == "tabitem",
                    destructive=False,
                    unsaved=False,
                    selection_count=1,
                )
            )

        return PrecisionTargetSnapshot(
            tuple(targets),
            snapshot.verification_state,
            snapshot.active_window_title,
            snapshot.active_window_handle,
            snapshot.truncated,
        )
