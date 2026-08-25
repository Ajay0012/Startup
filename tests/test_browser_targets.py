from __future__ import annotations

from pangu.browser_targets import BrowserTargetSnapshot, ChromeSemanticTargetAdapter


class Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


def test_normalized_rect_converts_screen_bounds_to_reference_space() -> None:
    normalized = ChromeSemanticTargetAdapter._normalized_rect(
        Rect(100, 20, 300, 70),
        Rect(0, 0, 1000, 500),
    )

    assert normalized == (0.1, 0.04, 0.2, 0.1)


def test_normalized_rect_handles_offset_virtual_desktop() -> None:
    normalized = ChromeSemanticTargetAdapter._normalized_rect(
        Rect(0, 100, 200, 200),
        Rect(-1920, 0, 1920, 1080),
    )

    assert normalized is not None
    x, y, width, height = normalized
    assert x == 0.5
    assert round(y, 6) == round(100 / 1080, 6)
    assert round(width, 6) == round(200 / 3840, 6)
    assert round(height, 6) == round(100 / 1080, 6)


def test_normalized_rect_rejects_empty_controls() -> None:
    assert (
        ChromeSemanticTargetAdapter._normalized_rect(
            Rect(100, 20, 100, 70),
            Rect(0, 0, 1000, 500),
        )
        is None
    )


def test_constructor_bounds_target_count() -> None:
    ChromeSemanticTargetAdapter(maximum_targets=1)
    ChromeSemanticTargetAdapter(maximum_targets=200)

    for invalid in (0, 201):
        try:
            ChromeSemanticTargetAdapter(maximum_targets=invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_target_shape_is_safe_for_spatial_controller() -> None:
    x, y, width, height = ChromeSemanticTargetAdapter._normalized_rect(
        Rect(35, 0, 355, 52),
        Rect(0, 0, 1920, 1200),
    ) or (0.0, 0.0, 0.0, 0.0)

    assert 0.0 <= x <= 1.0
    assert 0.0 <= y <= 1.0
    assert 0.0 < width <= 1.0
    assert 0.0 < height <= 1.0


def test_snapshot_tracks_foreground_state_without_changing_target_safety() -> None:
    snapshot = BrowserTargetSnapshot("chrome", "Chrome", 42, (), "VERIFIED", window_active=True)

    assert snapshot.window_active is True
    assert snapshot.targets == ()


def test_safe_returns_fallback_when_uia_callable_fails() -> None:
    def boom() -> str:
        raise RuntimeError("uia failed")

    assert ChromeSemanticTargetAdapter._safe(boom, "fallback") == "fallback"
