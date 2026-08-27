from pathlib import Path

import pytest

from pangu.gestures import GestureDetection, GestureKind, HandLandmark, HandObservation
from pangu.spatial_interaction import SemanticTarget
from pangu.spatial_live import GestureStabilizer, LiveSpatialDryRunRuntime, PointerMapper


def detection(kind: GestureKind, *, x: float = 0.2, y: float = 0.3) -> GestureDetection:
    metadata: dict[str, float | str] = {}
    if kind == GestureKind.POINT:
        metadata = {"x": x, "y": y}
    return GestureDetection(kind, 0.95, ("right-0",), 1.0, metadata)


def hand(*, fist: bool = False, timestamp: float = 1.0) -> HandObservation:
    points = [HandLandmark(0.5, 0.65, 0.0) for _ in range(21)]
    points[0] = HandLandmark(0.5, 0.72, 0.0)
    for index, x in zip((5, 9, 13, 17), (0.44, 0.49, 0.54, 0.59), strict=True):
        points[index] = HandLandmark(x, 0.58, 0.0)
    if fist:
        points[4] = HandLandmark(0.49, 0.57, 0.0)
        points[6] = HandLandmark(0.46, 0.57, 0.0)
        points[8] = HandLandmark(0.48, 0.60, 0.0)
        points[10] = HandLandmark(0.50, 0.57, 0.0)
        points[12] = HandLandmark(0.51, 0.61, 0.0)
        points[14] = HandLandmark(0.54, 0.57, 0.0)
        points[16] = HandLandmark(0.55, 0.61, 0.0)
        points[18] = HandLandmark(0.58, 0.57, 0.0)
        points[20] = HandLandmark(0.59, 0.61, 0.0)
    else:
        points[4] = HandLandmark(0.35, 0.48, 0.0)
        points[6] = HandLandmark(0.45, 0.46, 0.0)
        points[8] = HandLandmark(0.45, 0.24, 0.0)
        points[10] = HandLandmark(0.50, 0.48, 0.0)
        points[12] = HandLandmark(0.50, 0.62, 0.0)
        points[14] = HandLandmark(0.55, 0.48, 0.0)
        points[16] = HandLandmark(0.55, 0.62, 0.0)
        points[18] = HandLandmark(0.60, 0.48, 0.0)
        points[20] = HandLandmark(0.60, 0.62, 0.0)
    return HandObservation("right-0", "Right", tuple(points), timestamp, 0.95)


def runtime(**kwargs: object) -> LiveSpatialDryRunRuntime:
    return LiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
        **kwargs,
    )


def test_gesture_stabilizer_uses_stricter_pinch_than_grab() -> None:
    stabilizer = GestureStabilizer(grab_frames=2, open_palm_frames=2, pinch_frames=4)
    assert stabilizer.accept(detection(GestureKind.GRAB)) is False
    assert stabilizer.accept(detection(GestureKind.GRAB)) is True

    assert stabilizer.accept(detection(GestureKind.PINCH)) is False
    assert stabilizer.accept(detection(GestureKind.PINCH)) is False
    assert stabilizer.accept(detection(GestureKind.PINCH)) is False
    assert stabilizer.accept(detection(GestureKind.PINCH)) is True


def test_pointer_mapper_expands_camera_region_and_is_adaptive() -> None:
    mapper = PointerMapper(
        mirror_x=True,
        x_min=0.1,
        x_max=0.9,
        y_min=0.2,
        y_max=0.8,
        smoothing=0.58,
    )
    x, y = mapper.map(0.2, 0.5)
    assert round(x, 3) == 0.875
    assert round(y, 3) == 0.5
    moved_x, _ = mapper.map(0.8, 0.5)
    assert moved_x < x


def test_pointer_mapper_relative_delta_respects_mirroring() -> None:
    mapper = PointerMapper(
        mirror_x=True,
        x_min=0.1,
        x_max=0.9,
        y_min=0.2,
        y_max=0.8,
    )
    dx, dy = mapper.relative_delta(0.08, 0.06)
    assert dx == pytest.approx(-0.1)
    assert dy == pytest.approx(0.1)


def test_closed_fist_pose_wins_over_pinch_proximity() -> None:
    live = runtime()
    pose = live._manipulation_pose(hand(fist=True))
    assert pose is not None
    assert pose.gesture == GestureKind.GRAB


def test_fingertip_drives_pointer_independent_of_point_classifier() -> None:
    live = runtime(pointer_smoothing=1.0)
    point = live._point_from_fingertip(hand())
    assert point.gesture == GestureKind.POINT
    assert point.metadata["source"] == "index_fingertip"
    assert 0.0 <= float(point.metadata["x"]) <= 1.0
    assert 0.0 <= float(point.metadata["y"]) <= 1.0


def test_grab_drag_uses_relative_palm_delta_without_fingertip_jump() -> None:
    live = runtime(pointer_x_min=0.1, pointer_x_max=0.9, pointer_y_min=0.1, pointer_y_max=0.9)
    base = hand(fist=True)
    live._grab_anchor_raw = live._palm_anchor(base)
    live._grab_pointer_origin = (0.5, 0.5)

    shifted_points = tuple(
        HandLandmark(point.x + 0.08, point.y + 0.04, point.z) for point in base.landmarks
    )
    shifted = HandObservation("right-0", "Right", shifted_points, 1.1, 0.95)
    drag = live._drag_from_palm(shifted)

    assert drag is not None
    assert drag.metadata["source"] == "grab_palm_delta"
    assert float(drag.metadata["x"]) < 0.5
    assert float(drag.metadata["y"]) > 0.5


def test_interaction_targets_gain_camera_friendly_halo() -> None:
    live = runtime(target_padding=0.04)
    live._targets = (
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
    expanded = live._interaction_targets()[0]
    assert expanded.target_id == "tab-1"
    assert expanded.x == pytest.approx(0.16)
    assert expanded.y == pytest.approx(0.06)
    assert expanded.width == pytest.approx(0.28)
    assert expanded.height == pytest.approx(0.13)


def test_runtime_diagnostics_reports_continuous_spatial_counters() -> None:
    diagnostics = runtime().diagnostics()
    assert diagnostics.dry_run is True
    assert diagnostics.hover_hits == 0
    assert diagnostics.grab_begins == 0
    assert diagnostics.drag_updates == 0
    assert diagnostics.pointer_updates == 0
