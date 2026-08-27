from pangu.gestures import HandLandmark, HandObservation
from pangu.hand_geometry import build_dense_geometry, stable_index_direction


def hand() -> HandObservation:
    points = [HandLandmark(0.5, 0.8, 0.0) for _ in range(21)]
    points[0] = HandLandmark(0.5, 0.82, 0.0)
    points[5] = HandLandmark(0.47, 0.64, 0.0)
    points[6] = HandLandmark(0.46, 0.52, 0.0)
    points[7] = HandLandmark(0.45, 0.39, 0.0)
    points[8] = HandLandmark(0.44, 0.25, 0.0)
    points[9] = HandLandmark(0.50, 0.63, 0.0)
    points[13] = HandLandmark(0.54, 0.64, 0.0)
    points[17] = HandLandmark(0.58, 0.66, 0.0)
    return HandObservation("right-0", "Right", tuple(points), 1.0, 0.95)


def test_dense_geometry_has_42_control_points_but_only_21_measured() -> None:
    geometry = build_dense_geometry(hand())
    assert geometry.measured_count == 21
    assert geometry.derived_count == 21
    assert geometry.total_control_points == 42
    assert geometry.palm_scale > 0.0


def test_stable_index_direction_points_toward_tip() -> None:
    dx, dy = stable_index_direction(hand())
    assert dx < 0.0
    assert dy < 0.0
    assert abs((dx * dx + dy * dy) - 1.0) < 1e-6
