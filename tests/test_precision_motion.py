from pangu.precision_motion import PrecisionDragController, PrecisionTargetLock
from pangu.precision_targets import PrecisionSemanticTargetAdapter
from pangu.spatial_interaction import SemanticTarget


def target() -> SemanticTarget:
    return SemanticTarget(
        target_id="button-1",
        kind="ui_action",
        x=0.40,
        y=0.30,
        width=0.10,
        height=0.08,
    )


def test_slow_near_target_enters_precision_lock() -> None:
    lock = PrecisionTargetLock(capture_radius=0.06, release_radius=0.10)
    first = lock.apply(0.385, 0.34, 1.0, (target(),))
    second = lock.apply(0.388, 0.341, 1.05, (target(),))

    assert first.locked_target_id == "button-1"
    assert second.locked_target_id == "button-1"
    assert second.precision_mode is True
    assert second.x > 0.388


def test_target_lock_uses_release_hysteresis() -> None:
    lock = PrecisionTargetLock(capture_radius=0.05, release_radius=0.10)
    acquired = lock.apply(0.39, 0.34, 1.0, (target(),))
    still_locked = lock.apply(0.35, 0.34, 1.05, (target(),))
    released = lock.apply(0.20, 0.80, 1.10, (target(),))

    assert acquired.locked_target_id == "button-1"
    assert still_locked.locked_target_id == "button-1"
    assert released.locked_target_id is None


def test_precision_drag_reduces_small_motion_gain() -> None:
    drag = PrecisionDragController(fine_gain=0.30, normal_gain=0.70, fast_gain=1.10)
    drag.begin(0.50, 0.50, 0.50, 0.50)
    point = drag.update(
        0.505,
        0.500,
        x_span=0.80,
        y_span=0.80,
        mirror_x=False,
    )

    assert point is not None
    x, y = point
    assert 0.50 < x < 0.505 / 0.80 + 0.50
    assert y == 0.50


def test_precision_drag_scales_large_motion_and_mirrors_x() -> None:
    drag = PrecisionDragController()
    drag.begin(0.50, 0.50, 0.50, 0.50)
    point = drag.update(
        0.55,
        0.54,
        x_span=0.80,
        y_span=0.80,
        mirror_x=True,
    )

    assert point is not None
    x, y = point
    assert x < 0.50
    assert y > 0.50


def test_precision_target_kind_maps_actionable_controls() -> None:
    assert PrecisionSemanticTargetAdapter._target_kind("Button") == "ui_action"
    assert PrecisionSemanticTargetAdapter._target_kind("Edit") == "ui_input"
    assert PrecisionSemanticTargetAdapter._target_kind("TabItem") == "browser_tab"
