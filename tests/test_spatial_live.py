from pathlib import Path

from pangu.gestures import GestureDetection, GestureKind
from pangu.spatial_live import GestureStabilizer, LiveSpatialDryRunRuntime


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

    # A different pose resets the streak and permits a later stable transition.
    assert stabilizer.accept(detection(GestureKind.OPEN_PALM)) is False
    assert stabilizer.accept(detection(GestureKind.OPEN_PALM)) is False
    assert stabilizer.accept(detection(GestureKind.OPEN_PALM)) is True


def test_point_passes_through_stabilizer() -> None:
    stabilizer = GestureStabilizer(required_frames=3)
    assert stabilizer.accept(detection(GestureKind.POINT)) is True


def test_live_runtime_mirrors_point_x_only() -> None:
    runtime = LiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
        mirror_x=True,
    )
    transformed = runtime._screen_detection(detection(GestureKind.POINT, x=0.2, y=0.3))
    assert transformed.metadata["x"] == 0.8
    assert transformed.metadata["y"] == 0.3


def test_non_point_detection_is_not_rewritten() -> None:
    runtime = LiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
    )
    raw = detection(GestureKind.GRAB)
    assert runtime._screen_detection(raw) is raw


def test_runtime_diagnostics_explicitly_reports_dry_run() -> None:
    runtime = LiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
    )
    assert runtime.diagnostics().dry_run is True
