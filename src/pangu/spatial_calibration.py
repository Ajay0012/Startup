from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median


@dataclass(frozen=True)
class PointerCalibration:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    mirror_x: bool
    smoothing: float = 0.58

    def __post_init__(self) -> None:
        if not 0.0 <= self.x_min < self.x_max <= 1.0:
            raise ValueError("x calibration must satisfy 0 <= min < max <= 1")
        if not 0.0 <= self.y_min < self.y_max <= 1.0:
            raise ValueError("y calibration must satisfy 0 <= min < max <= 1")
        if not 0.0 < self.smoothing <= 1.0:
            raise ValueError("smoothing must be in (0, 1]")

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> PointerCalibration:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            x_min=float(data["x_min"]),
            x_max=float(data["x_max"]),
            y_min=float(data["y_min"]),
            y_max=float(data["y_max"]),
            mirror_x=bool(data["mirror_x"]),
            smoothing=float(data.get("smoothing", 0.58)),
        )


def derive_axis_calibration(
    left: list[tuple[float, float]],
    right: list[tuple[float, float]],
    top: list[tuple[float, float]],
    bottom: list[tuple[float, float]],
    *,
    margin: float = 0.02,
    smoothing: float = 0.58,
) -> PointerCalibration:
    """Calibrate from comfortable camera-visible hand positions, not screen corners.

    The user keeps the whole hand visible and moves the pointing fingertip to the
    left/right/top/bottom limits of a comfortable air-control rectangle. This is much
    more reliable for laptop webcams than physically pointing at display corners,
    which often moves the hand outside the camera field of view.
    """

    groups = (left, right, top, bottom)
    if any(len(group) < 3 for group in groups):
        raise ValueError("at least three fingertip samples are required for every direction")

    left_x = median([x for x, _ in left])
    right_x = median([x for x, _ in right])
    top_y = median([y for _, y in top])
    bottom_y = median([y for _, y in bottom])

    mirror_x = left_x > right_x
    raw_x_min, raw_x_max = sorted((left_x, right_x))
    raw_y_min, raw_y_max = sorted((top_y, bottom_y))

    x_min = max(0.0, raw_x_min - margin)
    x_max = min(1.0, raw_x_max + margin)
    y_min = max(0.0, raw_y_min - margin)
    y_max = min(1.0, raw_y_max + margin)

    if x_max - x_min < 0.10 or y_max - y_min < 0.10:
        raise ValueError(
            "calibration range is too small; move the hand farther apart while keeping it visible"
        )

    return PointerCalibration(x_min, x_max, y_min, y_max, mirror_x, smoothing)


def derive_pointer_calibration(
    top_left: list[tuple[float, float]],
    top_right: list[tuple[float, float]],
    bottom_left: list[tuple[float, float]],
    bottom_right: list[tuple[float, float]],
    *,
    margin: float = 0.015,
    smoothing: float = 0.58,
) -> PointerCalibration:
    """Backward-compatible four-corner calibration helper."""

    groups = (top_left, top_right, bottom_left, bottom_right)
    if any(len(group) < 3 for group in groups):
        raise ValueError("at least three fingertip samples are required for every corner")

    left = top_left + bottom_left
    right = top_right + bottom_right
    top = top_left + top_right
    bottom = bottom_left + bottom_right
    return derive_axis_calibration(
        left,
        right,
        top,
        bottom,
        margin=margin,
        smoothing=smoothing,
    )
