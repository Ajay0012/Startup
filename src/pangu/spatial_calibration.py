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
    smoothing: float = 0.42

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
    def load(cls, path: Path) -> "PointerCalibration":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            x_min=float(data["x_min"]),
            x_max=float(data["x_max"]),
            y_min=float(data["y_min"]),
            y_max=float(data["y_max"]),
            mirror_x=bool(data["mirror_x"]),
            smoothing=float(data.get("smoothing", 0.42)),
        )


def derive_pointer_calibration(
    top_left: list[tuple[float, float]],
    top_right: list[tuple[float, float]],
    bottom_left: list[tuple[float, float]],
    bottom_right: list[tuple[float, float]],
    *,
    margin: float = 0.015,
    smoothing: float = 0.42,
) -> PointerCalibration:
    groups = (top_left, top_right, bottom_left, bottom_right)
    if any(len(group) < 3 for group in groups):
        raise ValueError("at least three fingertip samples are required for every corner")

    left_x = median([x for x, _ in top_left + bottom_left])
    right_x = median([x for x, _ in top_right + bottom_right])
    top_y = median([y for _, y in top_left + top_right])
    bottom_y = median([y for _, y in bottom_left + bottom_right])

    mirror_x = left_x > right_x
    raw_x_min, raw_x_max = sorted((left_x, right_x))
    raw_y_min, raw_y_max = sorted((top_y, bottom_y))

    # A small inward margin prevents noisy corner samples from forcing the pointer
    # against the exact desktop edge on every frame.
    x_min = max(0.0, raw_x_min - margin)
    x_max = min(1.0, raw_x_max + margin)
    y_min = max(0.0, raw_y_min - margin)
    y_max = min(1.0, raw_y_max + margin)

    if x_max - x_min < 0.20 or y_max - y_min < 0.20:
        raise ValueError("calibration range is too small; point farther apart at the four corners")

    return PointerCalibration(x_min, x_max, y_min, y_max, mirror_x, smoothing)
