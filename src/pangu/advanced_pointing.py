from __future__ import annotations

from dataclasses import dataclass
from math import exp, hypot, pi
from time import monotonic

from .gestures import HandObservation
from .spatial_interaction import SemanticTarget


@dataclass(frozen=True)
class PointingEstimate:
    x: float
    y: float
    confidence: float
    source: str = "advanced-index-ray"


class OneEuroAxis:
    """Low-lag adaptive filter for hand pointing coordinates."""

    def __init__(self, *, min_cutoff: float = 1.6, beta: float = 0.045, d_cutoff: float = 1.0) -> None:
        if min_cutoff <= 0 or d_cutoff <= 0 or beta < 0:
            raise ValueError("invalid One Euro filter parameters")
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._value: float | None = None
        self._derivative = 0.0
        self._time: float | None = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * pi * cutoff)
        return 1.0 / (1.0 + tau / max(dt, 1e-6))

    def filter(self, value: float, timestamp: float) -> float:
        if self._value is None or self._time is None:
            self._value = value
            self._time = timestamp
            return value
        dt = max(1e-4, timestamp - self._time)
        raw_derivative = (value - self._value) / dt
        d_alpha = self._alpha(self.d_cutoff, dt)
        self._derivative += d_alpha * (raw_derivative - self._derivative)
        cutoff = self.min_cutoff + self.beta * abs(self._derivative)
        alpha = self._alpha(cutoff, dt)
        self._value += alpha * (value - self._value)
        self._time = timestamp
        return self._value

    def reset(self) -> None:
        self._value = None
        self._derivative = 0.0
        self._time = None


class AdvancedPointingEstimator:
    """Stable pointer estimate from the full index-finger chain, not only the fingertip.

    The estimator combines MCP→PIP, PIP→DIP and DIP→TIP vectors, rejects weak pointing
    poses, projects a short ray beyond the fingertip, applies low-lag One Euro filtering,
    and can softly snap near semantic UI targets. It consumes only landmarks and never
    stores camera frames.
    """

    def __init__(
        self,
        *,
        ray_gain: float = 0.22,
        min_confidence: float = 0.48,
        snap_radius: float = 0.035,
        snap_strength: float = 0.62,
    ) -> None:
        if not 0.0 <= ray_gain <= 1.0:
            raise ValueError("ray_gain must be between 0 and 1")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if not 0.0 <= snap_radius <= 0.2:
            raise ValueError("snap_radius must be between 0 and 0.2")
        if not 0.0 <= snap_strength <= 1.0:
            raise ValueError("snap_strength must be between 0 and 1")
        self.ray_gain = ray_gain
        self.min_confidence = min_confidence
        self.snap_radius = snap_radius
        self.snap_strength = snap_strength
        self._x_filter = OneEuroAxis()
        self._y_filter = OneEuroAxis()

    @staticmethod
    def _clamp(value: float) -> float:
        return min(1.0, max(0.0, value))

    @staticmethod
    def _finger_extension_score(hand: HandObservation) -> float:
        points = hand.landmarks
        wrist = points[0]
        mcp, pip, dip, tip = points[5], points[6], points[7], points[8]
        chain = hypot(pip.x - mcp.x, pip.y - mcp.y) + hypot(dip.x - pip.x, dip.y - pip.y) + hypot(tip.x - dip.x, tip.y - dip.y)
        direct = hypot(tip.x - mcp.x, tip.y - mcp.y)
        straightness = min(1.0, direct / max(chain, 1e-6))
        reach = min(1.0, hypot(tip.x - wrist.x, tip.y - wrist.y) / 0.42)
        return 0.65 * straightness + 0.35 * reach

    @staticmethod
    def _index_direction(hand: HandObservation) -> tuple[float, float]:
        points = hand.landmarks
        mcp, pip, dip, tip = points[5], points[6], points[7], points[8]
        vectors = (
            (pip.x - mcp.x, pip.y - mcp.y, 0.15),
            (dip.x - pip.x, dip.y - pip.y, 0.30),
            (tip.x - dip.x, tip.y - dip.y, 0.55),
        )
        dx = sum(vx * weight for vx, _, weight in vectors)
        dy = sum(vy * weight for _, vy, weight in vectors)
        norm = hypot(dx, dy)
        if norm <= 1e-6:
            return 0.0, 0.0
        return dx / norm, dy / norm

    def estimate(self, hand: HandObservation, *, timestamp: float | None = None) -> PointingEstimate | None:
        extension = self._finger_extension_score(hand)
        confidence = min(1.0, hand.confidence * (0.55 + 0.45 * extension))
        if confidence < self.min_confidence:
            return None

        tip = hand.landmarks[8]
        dip = hand.landmarks[7]
        segment = hypot(tip.x - dip.x, tip.y - dip.y)
        dx, dy = self._index_direction(hand)
        raw_x = self._clamp(tip.x + dx * segment * self.ray_gain)
        raw_y = self._clamp(tip.y + dy * segment * self.ray_gain)
        now = monotonic() if timestamp is None else timestamp
        x = self._x_filter.filter(raw_x, now)
        y = self._y_filter.filter(raw_y, now)
        return PointingEstimate(x, y, confidence)

    def snap(self, x: float, y: float, targets: tuple[SemanticTarget, ...]) -> tuple[float, float, str | None]:
        if not targets or self.snap_radius <= 0.0:
            return x, y, None
        best: tuple[float, SemanticTarget, float, float] | None = None
        for target in targets:
            cx = target.x + target.width / 2.0
            cy = target.y + target.height / 2.0
            nearest_x = min(max(x, target.x), target.x + target.width)
            nearest_y = min(max(y, target.y), target.y + target.height)
            distance = hypot(nearest_x - x, nearest_y - y)
            if distance <= self.snap_radius and (best is None or distance < best[0]):
                best = (distance, target, cx, cy)
        if best is None:
            return x, y, None
        distance, target, cx, cy = best
        proximity = 1.0 - distance / max(self.snap_radius, 1e-6)
        strength = self.snap_strength * proximity
        snapped_x = x + (cx - x) * strength
        snapped_y = y + (cy - y) * strength
        return self._clamp(snapped_x), self._clamp(snapped_y), target.target_id

    def reset(self) -> None:
        self._x_filter.reset()
        self._y_filter.reset()
