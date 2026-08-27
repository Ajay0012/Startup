from pangu.gestures import (
    GestureKind,
    HandLandmark,
    HandObservation,
    TemporalGestureRecognizer,
)


def _hand(*, wrist_x: float = 0.5, wrist_y: float = 0.8, pinch: bool = False) -> HandObservation:
    points = [HandLandmark(wrist_x, wrist_y, 0.0) for _ in range(21)]
    points[6] = HandLandmark(0.45, 0.55)
    points[8] = HandLandmark(0.45, 0.30)
    points[10] = HandLandmark(0.50, 0.55)
    points[12] = HandLandmark(0.50, 0.75)
    points[14] = HandLandmark(0.55, 0.55)
    points[16] = HandLandmark(0.55, 0.75)
    points[18] = HandLandmark(0.60, 0.55)
    points[20] = HandLandmark(0.60, 0.75)
    points[4] = HandLandmark(0.455 if pinch else 0.25, 0.305 if pinch else 0.50)
    return HandObservation("right-0", "Right", tuple(points), 1.0, 0.95)


def test_recognizes_pinch() -> None:
    recognizer = TemporalGestureRecognizer()
    detection = recognizer.recognize((_hand(pinch=True),))
    assert detection[0].gesture == GestureKind.PINCH


def test_recognizes_point() -> None:
    recognizer = TemporalGestureRecognizer()
    detection = recognizer.recognize((_hand(),))
    assert detection[0].gesture == GestureKind.POINT


def test_recognizes_horizontal_swipe() -> None:
    recognizer = TemporalGestureRecognizer()
    first = _hand(wrist_x=0.2)
    second = HandObservation(
        first.hand_id,
        first.handedness,
        tuple(HandLandmark(point.x + 0.35, point.y, point.z) for point in first.landmarks),
        1.3,
        first.confidence,
    )
    recognizer.recognize((first,))
    detection = recognizer.recognize((second,))
    assert detection[0].gesture == GestureKind.SWIPE_RIGHT


def test_invalid_landmark_count_is_rejected() -> None:
    try:
        HandObservation("bad", "Right", (HandLandmark(0.0, 0.0),), 0.0)
    except ValueError as error:
        assert "21 landmarks" in str(error)
    else:
        raise AssertionError("invalid hand observation was accepted")
