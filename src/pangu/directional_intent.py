from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from .spatial_interaction import SemanticTarget


@dataclass(frozen=True)
class IntentPoint:
    x: float
    y: float
    target_id: str | None
    speed: float
    projected_x: float
    projected_y: float


class DirectionalIntentAssist:
    """Predict likely UI intent from pointer direction, travel length and speed.

    The assist is deliberately bounded: it only biases toward a semantic target when
    the recent movement vector projects close to that target. It never invents clicks
    or destructive actions.
    """

    def __init__(
        self,
        *,
        horizon_seconds: float = 0.24,
        corridor_radius: float = 0.085,
        maximum_strength: float = 0.58,
    ) -> None:
        if not 0.05 <= horizon_seconds <= 0.8:
            raise ValueError("horizon_seconds must be between 0.05 and 0.8")
        if not 0.01 <= corridor_radius <= 0.25:
            raise ValueError("corridor_radius must be between 0.01 and 0.25")
        if not 0.0 <= maximum_strength <= 1.0:
            raise ValueError("maximum_strength must be between 0 and 1")
        self.horizon_seconds = horizon_seconds
        self.corridor_radius = corridor_radius
        self.maximum_strength = maximum_strength
        self._last: tuple[float, float, float] | None = None

    @staticmethod
    def _clamp(value: float) -> float:
        return min(1.0, max(0.0, value))

    def apply(
        self,
        x: float,
        y: float,
        timestamp: float,
        targets: tuple[SemanticTarget, ...],
    ) -> IntentPoint:
        previous = self._last
        self._last = (timestamp, x, y)
        if previous is None:
            return IntentPoint(x, y, None, 0.0, x, y)
        dt = max(1e-4, timestamp - previous[0])
        vx = (x - previous[1]) / dt
        vy = (y - previous[2]) / dt
        speed = hypot(vx, vy)
        if speed < 0.025 or not targets:
            return IntentPoint(x, y, None, speed, x, y)

        horizon = min(0.38, self.horizon_seconds + min(speed, 1.5) * 0.06)
        projected_x = self._clamp(x + vx * horizon)
        projected_y = self._clamp(y + vy * horizon)

        best: tuple[float, SemanticTarget, float, float] | None = None
        travel_x = projected_x - x
        travel_y = projected_y - y
        travel_length = hypot(travel_x, travel_y)
        if travel_length < 1e-6:
            return IntentPoint(x, y, None, speed, projected_x, projected_y)

        for target in targets:
            cx = target.x + target.width / 2.0
            cy = target.y + target.height / 2.0
            tx = cx - x
            ty = cy - y
            forward = (tx * travel_x + ty * travel_y) / travel_length
            if forward <= 0.0:
                continue
            # Perpendicular distance to the forward motion ray.
            perpendicular = abs(tx * travel_y - ty * travel_x) / travel_length
            distance = hypot(tx, ty)
            dynamic_corridor = self.corridor_radius + min(0.06, speed * 0.025)
            if perpendicular > dynamic_corridor or distance > 0.55:
                continue
            score = perpendicular + distance * 0.20
            if best is None or score < best[0]:
                best = (score, target, cx, cy)

        if best is None:
            return IntentPoint(x, y, None, speed, projected_x, projected_y)

        _, target, cx, cy = best
        distance_to_projection = hypot(projected_x - cx, projected_y - cy)
        proximity = max(0.0, 1.0 - distance_to_projection / 0.30)
        strength = min(self.maximum_strength, 0.18 + proximity * self.maximum_strength)
        assisted_x = self._clamp(x + (cx - x) * strength)
        assisted_y = self._clamp(y + (cy - y) * strength)
        return IntentPoint(
            assisted_x,
            assisted_y,
            target.target_id,
            speed,
            projected_x,
            projected_y,
        )

    def reset(self) -> None:
        self._last = None
