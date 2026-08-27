from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from .gestures import HandLandmark, HandObservation


# MediaPipe/YOLO expose the same 21 anatomical landmarks.  These 20 edges are the
# standard hand skeleton connections.  Midpoints are derived features, not extra
# camera detections.
HAND_BONES: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
)


@dataclass(frozen=True)
class DenseHandGeometry:
    """21 measured landmarks plus derived geometry used for stable control.

    ``control_points`` contains 42 points: the 21 model landmarks, 20 bone
    midpoints, and one palm center.  The additional 21 points are deterministic
    geometry and must never be described as independently detected landmarks.
    """

    control_points: tuple[HandLandmark, ...]
    palm_center: HandLandmark
    palm_scale: float

    @property
    def measured_count(self) -> int:
        return 21

    @property
    def derived_count(self) -> int:
        return len(self.control_points) - self.measured_count

    @property
    def total_control_points(self) -> int:
        return len(self.control_points)


def _midpoint(a: HandLandmark, b: HandLandmark) -> HandLandmark:
    return HandLandmark((a.x + b.x) / 2.0, (a.y + b.y) / 2.0, (a.z + b.z) / 2.0)


def build_dense_geometry(hand: HandObservation) -> DenseHandGeometry:
    points = hand.landmarks
    palm_ids = (0, 5, 9, 13, 17)
    palm_center = HandLandmark(
        sum(points[index].x for index in palm_ids) / len(palm_ids),
        sum(points[index].y for index in palm_ids) / len(palm_ids),
        sum(points[index].z for index in palm_ids) / len(palm_ids),
    )
    midpoints = tuple(_midpoint(points[a], points[b]) for a, b in HAND_BONES)
    palm_scale = (
        hypot(points[5].x - points[17].x, points[5].y - points[17].y)
        + hypot(points[0].x - points[9].x, points[0].y - points[9].y)
    ) / 2.0
    return DenseHandGeometry(points + midpoints + (palm_center,), palm_center, palm_scale)


def stable_index_direction(hand: HandObservation) -> tuple[float, float]:
    """Estimate index direction from seven samples along the whole finger axis.

    The samples include the four measured index joints and three segment midpoints.
    A weighted centroid-to-tip fit is less sensitive to a single noisy fingertip
    than using DIP->TIP alone while still preserving the intended finger direction.
    """

    p = hand.landmarks
    mcp, pip, dip, tip = p[5], p[6], p[7], p[8]
    samples = (
        (mcp, 0.55),
        (_midpoint(mcp, pip), 0.70),
        (pip, 0.85),
        (_midpoint(pip, dip), 1.00),
        (dip, 1.15),
        (_midpoint(dip, tip), 1.30),
        (tip, 1.45),
    )
    total = sum(weight for _, weight in samples)
    cx = sum(point.x * weight for point, weight in samples) / total
    cy = sum(point.y * weight for point, weight in samples) / total
    dx = tip.x - cx
    dy = tip.y - cy
    norm = hypot(dx, dy)
    if norm <= 1e-6:
        return 0.0, 0.0
    return dx / norm, dy / norm
