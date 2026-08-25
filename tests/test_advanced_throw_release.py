from pathlib import Path

from pangu.gestures import GestureKind, HandLandmark, HandObservation
from pangu.spatial_live_advanced import AdvancedLiveSpatialDryRunRuntime


def _runtime() -> AdvancedLiveSpatialDryRunRuntime:
    return AdvancedLiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
    )


def _open_hand_three_fingers() -> HandObservation:
    points = [HandLandmark(0.5, 0.7, 0.0) for _ in range(21)]
    points[0] = HandLandmark(0.5, 0.82, 0.0)
    # index, middle, ring clearly extended; little finger intentionally imperfect
    points[6] = HandLandmark(0.44, 0.50, 0.0)
    points[8] = HandLandmark(0.44, 0.26, 0.0)
    points[10] = HandLandmark(0.50, 0.50, 0.0)
    points[12] = HandLandmark(0.50, 0.24, 0.0)
    points[14] = HandLandmark(0.56, 0.52, 0.0)
    points[16] = HandLandmark(0.56, 0.28, 0.0)
    points[18] = HandLandmark(0.62, 0.48, 0.0)
    points[20] = HandLandmark(0.62, 0.55, 0.0)
    points[4] = HandLandmark(0.32, 0.52, 0.0)
    return HandObservation("right-0", "Right", tuple(points), 1.0, 0.95)


def test_advanced_release_accepts_three_extended_fingers() -> None:
    runtime = _runtime()
    pose = runtime._manipulation_pose(_open_hand_three_fingers())
    assert pose is not None
    assert pose.gesture == GestureKind.OPEN_PALM


def test_advanced_open_palm_release_has_single_frame_gate() -> None:
    runtime = _runtime()
    pose = runtime._manipulation_pose(_open_hand_three_fingers())
    assert pose is not None
    assert runtime.stabilizer.accept(pose) is True
