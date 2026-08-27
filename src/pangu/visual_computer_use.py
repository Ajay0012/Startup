from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from typing import Protocol

from .computer_use import (
    ComputerActionKind,
    ComputerActionRequest,
    ComputerActionResult,
    ComputerUseState,
)
from .screen_vision import OcrTextRegion, ScreenVisionRuntime


class ResolvedPointerAdapter(Protocol):
    def click(self, x: int, y: int) -> bool: ...


class WindowsResolvedPointerAdapter:
    """Windows pointer injection used only with deterministic resolved coordinates."""

    def click(self, x: int, y: int) -> bool:
        if os.name != "nt":
            return False
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            if not user32.SetCursorPos(int(x), int(y)):
                return False
            mouse_down = 0x0002
            mouse_up = 0x0004
            user32.mouse_event(mouse_down, 0, 0, 0, 0)
            user32.mouse_event(mouse_up, 0, 0, 0, 0)
            return True
        except (AttributeError, OSError):
            return False


@dataclass(frozen=True)
class VisualSafetyPolicy:
    minimum_confidence: float = 0.68
    postcondition_delay_seconds: float = 0.12


class VisualComputerUseFallback:
    """OCR-grounded visual fallback with before/after verification.

    Coordinates never originate from a language model. They are derived from a
    high-confidence, unambiguous OCR region in a fresh screenshot and validated to be
    inside the captured desktop bounds. Consequential labels remain denied.
    """

    _consequential = frozenset(
        {
            "buy",
            "purchase",
            "pay",
            "send",
            "submit",
            "delete",
            "remove",
            "uninstall",
            "install",
            "authorize",
            "allow",
            "grant",
            "transfer",
            "withdraw",
            "confirm order",
        }
    )

    def __init__(
        self,
        vision: ScreenVisionRuntime,
        pointer: ResolvedPointerAdapter | None = None,
        policy: VisualSafetyPolicy | None = None,
    ) -> None:
        self.vision = vision
        self.pointer = pointer or WindowsResolvedPointerAdapter()
        self.policy = policy or VisualSafetyPolicy()

    @classmethod
    def _sensitive_label(cls, text: str) -> bool:
        normalized = " ".join(text.casefold().split())
        return any(term in normalized for term in cls._consequential)

    @staticmethod
    def _inside(region: OcrTextRegion, width: int, height: int) -> bool:
        return (
            region.x >= 0
            and region.y >= 0
            and region.width > 0
            and region.height > 0
            and region.x + region.width <= width
            and region.y + region.height <= height
        )

    def execute(self, request: ComputerActionRequest) -> ComputerActionResult:
        if request.action != ComputerActionKind.INVOKE:
            return ComputerActionResult(
                request.action,
                ComputerUseState.UNSUPPORTED,
                "Visual fallback currently supports guarded invoke/click only.",
                normalized_error="VISUAL_ACTION_UNSUPPORTED",
            )
        query = request.target.name.strip()
        if not query:
            return ComputerActionResult(
                request.action,
                ComputerUseState.DENIED,
                "Visual fallback requires a text-grounded target.",
                normalized_error="VISUAL_TARGET_REQUIRED",
            )
        if self._sensitive_label(query):
            return ComputerActionResult(
                request.action,
                ComputerUseState.DENIED,
                "Consequential visual controls require a dedicated approval flow.",
                normalized_error="VISUAL_CONSEQUENTIAL_CONTROL_BLOCKED",
            )
        try:
            before, text_regions = self.vision.snapshot()
        except RuntimeError as error:
            return ComputerActionResult(
                request.action,
                ComputerUseState.UNSUPPORTED,
                "Visual perception is unavailable.",
                normalized_error=str(error),
            )
        region = self.vision.resolve_text_target(
            query,
            text_regions,
            minimum_confidence=self.policy.minimum_confidence,
        )
        if region is None:
            return ComputerActionResult(
                request.action,
                ComputerUseState.UNSUPPORTED,
                "No unambiguous high-confidence visual target was found.",
                normalized_error="VISUAL_TARGET_NOT_FOUND_OR_AMBIGUOUS",
            )
        if self._sensitive_label(region.text) or not self._inside(
            region, before.width, before.height
        ):
            return ComputerActionResult(
                request.action,
                ComputerUseState.DENIED,
                "Resolved visual target failed safety validation.",
                normalized_error="VISUAL_TARGET_SAFETY_REJECTED",
            )
        x = region.x + region.width // 2
        y = region.y + region.height // 2
        if not self.pointer.click(x, y):
            return ComputerActionResult(
                request.action,
                ComputerUseState.FAILED,
                "Resolved pointer action failed.",
                normalized_error="VISUAL_POINTER_ACTION_FAILED",
            )
        time.sleep(self.policy.postcondition_delay_seconds)
        try:
            after, _ = self.vision.snapshot()
        except RuntimeError:
            return ComputerActionResult(
                request.action,
                ComputerUseState.UNVERIFIED,
                "Visual action executed but the postcondition could not be observed.",
                evidence={"resolved_x": x, "resolved_y": y, "target_text": region.text},
                normalized_error="VISUAL_POSTCONDITION_UNAVAILABLE",
            )
        changed = after.frame_hash != before.frame_hash
        return ComputerActionResult(
            request.action,
            ComputerUseState.VERIFIED if changed else ComputerUseState.UNVERIFIED,
            "Visual action executed from a freshly resolved OCR target.",
            evidence={
                "resolved_x": x,
                "resolved_y": y,
                "target_text": region.text,
                "target_confidence": region.confidence,
                "before_hash": before.frame_hash,
                "after_hash": after.frame_hash,
                "screen_changed": changed,
            },
            normalized_error=None if changed else "VISUAL_POSTCONDITION_UNCHANGED",
        )
