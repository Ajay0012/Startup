from pangu.advanced_pointing import AdvancedPointingEstimator, OneEuroAxis
from pangu.gestures import HandLandmark, HandObservation
from pangu.spatial_interaction import SemanticTarget


def hand() -> HandObservation:
    points = [HandLandmark(0.5, 0.8, 0.0) for _ in range(21)]
    points[0] = HandLandmark(0.5, 0.8, 0.0)
    points[5] = HandLandmark(0.48, 0.62, 0.0)
    points[6] = HandLandmark(0.47, 0.50, 0.0)
    points[7] = HandLandmark(0.46, 0.38, 0.0)
    points[8] = HandLandmark(0.45, 0.25, 0.0)
    points[4] = HandLandmark(0.35, 0.55, 0.0)
    return HandObservation("right-0", "Right", tuple(points), 1.0, 0.95)


def test_one_euro_filter_tracks_without_overshoot() -> None:
    axis = OneEuroAxis(min_cutoff=1.0, beta=0.05)
    first = axis.filter(0.2, 1.0)
    second = axis.filter(0.8, 1.033)
    assert first == 0.2
    assert 0.2 < second <= 0.8


def test_advanced_estimator_uses_index_ray() -> None:
    estimator = AdvancedPointingEstimator(ray_gain=0.2)
    estimate = estimator.estimate(hand(), timestamp=1.0)
    assert estimate is not None
    assert 0.0 <= estimate.x <= 1.0
    assert 0.0 <= estimate.y <= 1.0
    assert estimate.y < hand().landmarks[8].y
    assert estimate.confidence > 0.5


def test_semantic_snap_assists_nearby_target_only() -> None:
    estimator = AdvancedPointingEstimator(snap_radius=0.05, snap_strength=0.8)
    target = SemanticTarget("tab", "browser_tab", 0.40, 0.10, 0.20, 0.08, closable=True)
    x, y, target_id = estimator.snap(0.39, 0.14, (target,))
    assert target_id == "tab"
    assert x > 0.39
    assert 0.10 <= y <= 0.18

    x2, y2, target_id2 = estimator.snap(0.1, 0.8, (target,))
    assert (x2, y2, target_id2) == (0.1, 0.8, None)
