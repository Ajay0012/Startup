from pathlib import Path

from pangu.spatial_calibration import (
    PointerCalibration,
    derive_axis_calibration,
    derive_pointer_calibration,
)


def cloud(x: float, y: float) -> list[tuple[float, float]]:
    return [
        (x - 0.002, y + 0.001),
        (x, y),
        (x + 0.002, y - 0.001),
        (x + 0.001, y + 0.002),
    ]


def test_derives_bounds_and_mirror_from_camera_visible_axis_poses() -> None:
    calibration = derive_axis_calibration(
        cloud(0.78, 0.50),
        cloud(0.22, 0.50),
        cloud(0.50, 0.22),
        cloud(0.50, 0.78),
        margin=0.01,
        smoothing=0.58,
    )

    assert calibration.mirror_x is True
    assert 0.20 < calibration.x_min < 0.23
    assert 0.77 < calibration.x_max < 0.80
    assert 0.20 < calibration.y_min < 0.23
    assert 0.77 < calibration.y_max < 0.80
    assert calibration.smoothing == 0.58


def test_derives_bounds_and_mirror_from_four_corners() -> None:
    calibration = derive_pointer_calibration(
        cloud(0.78, 0.20),
        cloud(0.22, 0.21),
        cloud(0.77, 0.80),
        cloud(0.23, 0.79),
        margin=0.01,
        smoothing=0.4,
    )

    assert calibration.mirror_x is True
    assert 0.20 < calibration.x_min < 0.23
    assert 0.77 < calibration.x_max < 0.80
    assert 0.18 < calibration.y_min < 0.22
    assert 0.79 < calibration.y_max < 0.82
    assert calibration.smoothing == 0.4


def test_calibration_round_trip(tmp_path: Path) -> None:
    original = PointerCalibration(0.2, 0.8, 0.15, 0.85, True, 0.58)
    path = tmp_path / "pointer.json"
    original.save(path)
    assert PointerCalibration.load(path) == original


def test_rejects_tiny_calibration_range() -> None:
    samples = cloud(0.5, 0.5)
    for derive in (derive_axis_calibration, derive_pointer_calibration):
        try:
            derive(samples, samples, samples, samples)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for tiny calibration range")
