from pathlib import Path

import pytest

from pangu.gestures import GestureDetection, GestureKind
from pangu.spatial_interaction import SemanticTarget
from pangu.spatial_live import GestureStabilizer, LiveSpatialDryRunRuntime, PointerMapper


def detection(kind: GestureKind, *, x: float = 0.2, y: float = 0.3) -> GestureDetection:
    metadata: dict[str, float | str] = {}
    if kind == GestureKind.POINT:
        metadata = {"x": x, "y": y}
    return GestureDetection(kind, 0.95, ("right-0",), 1.0, metadata)


def test_discrete_gestures_require_stable_streak_and_emit_once() -> None:
    stabilizer = GestureStabilizer(required_frames=3)
    assert stabilizer.accept(detection(GestureKind.GRAB)) is False
    assert stabilizer.accept(detection(GestureKind.GRAB)) is False
    assert stabilizer.accept(detection(GestureKind.GRAB)) is True
    assert stabilizer.accept(detection(GestureKind.GRAB)) is False

    assert stabilizer.accept(detection(GestureKind.OPEN_PALM)) is False
    assert stabilizer.accept(detection(GestureKind.OPEN_PALM)) is False
    assert stabilizer.accept(detection(GestureKind.OPEN_PALM)) is True


def test_default_stabilizer_accepts_two_frame_grab() -> None:
    stabilizer = GestureStabilizer()
    assert stabilizer.accept(detection(GestureKind.GRAB)) is False
    assert stabilizer.accept(detection(GestureKind.GRAB)) is True


def test_point_passes_through_stabilizer() -> None:
    stabilizer = GestureStabilizer(required_frames=3)
    assert stabilizer.accept(detection(GestureKind.POINT)) is True


def test_pointer_mapper_expands_natural_camera_region_to_screen() -> None:
    mapper = PointerMapper(
        mirror_x=True,
        x_min=0.1,
        x_max=0.9,
        y_min=0.2,
        y_max=0.8,
        smoothing=1.0,
    )
    x, y = mapper.map(0.2, 0.5)
    assert round(x, 3) == 0.875
    assert round(y, 3) == 0.5


def test_pointer_mapper_clamps_edges_and_can_disable_mirroring() -> None:
    mapper = PointerMapper(
        mirror_x=False,
        x_min=0.2,
        x_max=0.8,
        y_min=0.2,
        y_max=0.8,
        smoothing=1.0,
    )
    assert mapper.map(0.0, 1.0) == (0.0, 1.0)


def test_live_runtime_maps_point_into_calibrated_screen_space() -> None:
    runtime = LiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
        mirror_x=True,
        pointer_x_min=0.1,
        pointer_x_max=0.9,
        pointer_y_min=0.2,
        pointer_y_max=0.8,
        pointer_smoothing=1.0,
    )
    transformed = runtime._screen_detection(detection(GestureKind.POINT, x=0.2, y=0.5))
    assert round(float(transformed.metadata["x"]), 3) == 0.875
    assert round(float(transformed.metadata["y"]), 3) == 0.5


def test_non_point_detection_is_not_rewritten() -> None:
    runtime = LiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
    )
    raw = detection(GestureKind.GRAB)
    assert runtime._screen_detection(raw) is raw


def test_interaction_targets_gain_small_acquisition_halo() -> None:
    runtime = LiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
        target_padding=0.02,
    )
    runtime._targets = (
        SemanticTarget(
            target_id="tab-1",
            kind="browser_tab",
            x=0.2,
            y=0.1,
            width=0.2,
            height=0.05,
            closable=True,
        ),
    )
    expanded = runtime._interaction_targets()[0]
    assert expanded.target_id == "tab-1"
    assert expanded.x == pytest.approx(0.18)
    assert expanded.y == pytest.approx(0.08)
    assert expanded.width == pytest.approx(0.24)
    assert expanded.height == pytest.approx(0.09)


def test_runtime_diagnostics_explicitly_reports_dry_run() -> None:
    runtime = LiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
    )
    diagnostics = runtime.diagnostics()
    assert diagnostics.dry_run is True
    assert diagnostics.hover_hits == 0
    assert diagnostics.grab_begins == 0
