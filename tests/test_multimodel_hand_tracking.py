from pangu.gestures import HandLandmark, HandObservation
from pangu.multimodel_hand_tracking import HandTrajectoryPredictor, LandmarkFusion


def hand(x: float, y: float, *, confidence: float = 0.9, timestamp: float = 1.0) -> HandObservation:
    points = tuple(HandLandmark(x + index * 0.0005, y + index * 0.0003, 0.0) for index in range(21))
    return HandObservation("right-0", "Right", points, timestamp, confidence)


def test_fusion_blends_two_21_keypoint_models() -> None:
    first = hand(0.40, 0.50, confidence=0.9)
    second = hand(0.44, 0.52, confidence=0.7)
    fused = LandmarkFusion.fuse(first, second)
    assert 0.40 < fused.landmarks[0].x < 0.44
    assert 0.50 < fused.landmarks[0].y < 0.52
    assert fused.confidence > 0.7
    assert len(fused.landmarks) == 21


def test_matching_uses_nearest_palm_center() -> None:
    p1 = hand(0.20, 0.30)
    p2 = HandObservation("left-0", "Left", hand(0.75, 0.40).landmarks, 1.0, 0.9)
    s1 = HandObservation("yolo-0", "Unknown", hand(0.22, 0.31).landmarks, 1.0, 0.8)
    s2 = HandObservation("yolo-1", "Unknown", hand(0.74, 0.42).landmarks, 1.0, 0.8)
    pairs = LandmarkFusion.match((p1, p2), (s1, s2))
    assert len(pairs) == 2
    assert pairs[0][1].hand_id == "yolo-0"
    assert pairs[1][1].hand_id == "yolo-1"


def test_predictor_continues_motion_for_short_camera_loss() -> None:
    predictor = HandTrajectoryPredictor(horizon_seconds=0.5, damping=0.9)
    predictor.observe(hand(0.30, 0.50, timestamp=1.0))
    predictor.observe(hand(0.36, 0.50, timestamp=1.1))
    predicted = predictor.predict(1.2)
    assert predicted is not None
    assert predicted.landmarks[0].x > 0.36
    assert predicted.confidence < 0.9


def test_predictor_stops_after_bounded_horizon() -> None:
    predictor = HandTrajectoryPredictor(horizon_seconds=0.4)
    predictor.observe(hand(0.30, 0.50, timestamp=1.0))
    predictor.observe(hand(0.36, 0.50, timestamp=1.1))
    assert predictor.predict(1.6) is None
