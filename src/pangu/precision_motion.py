from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from .spatial_interaction import SemanticTarget


@dataclass(frozen=True)
class PrecisionPoint:
    x: float
    y: float
    locked_target_id: str | None
    precision_mode: bool


class PrecisionTargetLock:
    """Velocity-aware semantic target lock with capture/release hysteresis.

    Near an actionable UI element, slow deliberate pointing gets stronger assistance.
    Fast motion stays free so the cursor can cross the screen naturally. Once captured,
    a wider release radius prevents target flicker from small landmark jitter.
    """

    def __init__(
        self,
        *,
        capture_radius: float = 0.055,
        release_radius: float = 0.095,
        slow_speed: float = 0.32,
        base_strength: float = 0.34,
        precision_strength: float = 0.82,
    ) -> None:
        if not 0.0 < capture_radius < release_radius <= 0.25:
            raise ValueError("target lock radii are invalid")
        if slow_speed <= 0.0:
            raise ValueError("slow_speed must be positive")
        self.capture_radius = capture_radius
        self.release_radius = release_radius
        self.slow_speed = slow_speed
        self.base_strength = base_strength
        self.precision_strength = precision_strength
        self._locked_target_id: str | None = None
        self._last_x: float | None = None
        self._last_y: float | None = None
        self._last_time: float | None = None

    @staticmethod
    def _clamp(value: float) -> float:
        return min(1.0, max(0.0, value))

    @staticmethod
    def _nearest_point(x: float, y: float, target: SemanticTarget) -> tuple[float, float]:
        return (
            min(max(x, target.x), target.x + target.width),
            min(max(y, target.y), target.y + target.height),
        )

    @staticmethod
    def _center(target: SemanticTarget) -> tuple[float, float]:
        return target.x + target.width / 2.0, target.y + target.height / 2.0

    def _speed(self, x: float, y: float, timestamp: float) -> float:
        if self._last_x is None or self._last_y is None or self._last_time is None:
            speed = 0.0
        else:
            dt = max(1e-4, timestamp - self._last_time)
            speed = hypot(x - self._last_x, y - self._last_y) / dt
        self._last_x, self._last_y, self._last_time = x, y, timestamp
        return speed

    def apply(
        self,
        x: float,
        y: float,
        timestamp: float,
        targets: tuple[SemanticTarget, ...],
    ) -> PrecisionPoint:
        speed = self._speed(x, y, timestamp)

        locked = next(
            (target for target in targets if target.target_id == self._locked_target_id),
            None,
        )
        if locked is not None:
            nx, ny = self._nearest_point(x, y, locked)
            distance = hypot(nx - x, ny - y)
            if distance > self.release_radius:
                self._locked_target_id = None
                locked = None

        if locked is None:
            best: tuple[float, SemanticTarget] | None = None
            for target in targets:
                nx, ny = self._nearest_point(x, y, target)
                distance = hypot(nx - x, ny - y)
                if distance <= self.capture_radius and (best is None or distance < best[0]):
                    best = (distance, target)
            if best is not None:
                locked = best[1]
                self._locked_target_id = locked.target_id

        if locked is None:
            return PrecisionPoint(self._clamp(x), self._clamp(y), None, False)

        cx, cy = self._center(locked)
        slow_factor = max(0.0, min(1.0, 1.0 - speed / self.slow_speed))
        strength = self.base_strength + (self.precision_strength - self.base_strength) * slow_factor
        out_x = x + (cx - x) * strength
        out_y = y + (cy - y) * strength
        return PrecisionPoint(
            self._clamp(out_x),
            self._clamp(out_y),
            locked.target_id,
            slow_factor >= 0.45,
        )

    def reset(self) -> None:
        self._locked_target_id = None
        self._last_x = None
        self._last_y = None
        self._last_time = None


class PrecisionDragController:
    """Incremental clutch-like palm drag for exact placement.

    Small palm movements get reduced gain for precision. Larger intentional motions get
    more gain for reach. Incremental deltas avoid the sensitivity jump of mapping the
    whole drag against the original grab anchor.
    """

    def __init__(
        self,
        *,
        deadzone: float = 0.0012,
        fine_gain: float = 0.32,
        normal_gain: float = 0.72,
        fast_gain: float = 1.10,
    ) -> None:
        self.deadzone = deadzone
        self.fine_gain = fine_gain
        self.normal_gain = normal_gain
        self.fast_gain = fast_gain
        self._anchor: tuple[float, float] | None = None
        self._pointer: tuple[float, float] | None = None

    @staticmethod
    def _clamp(value: float) -> float:
        return min(1.0, max(0.0, value))

    def begin(self, palm_x: float, palm_y: float, pointer_x: float, pointer_y: float) -> None:
        self._anchor = (palm_x, palm_y)
        self._pointer = (pointer_x, pointer_y)

    def update(
        self,
        palm_x: float,
        palm_y: float,
        *,
        x_span: float,
        y_span: float,
        mirror_x: bool,
    ) -> tuple[float, float] | None:
        if self._anchor is None or self._pointer is None:
            return None
        dx = palm_x - self._anchor[0]
        dy = palm_y - self._anchor[1]
        self._anchor = (palm_x, palm_y)
        magnitude = hypot(dx, dy)
        if magnitude < self.deadzone:
            return self._pointer

        if magnitude < 0.008:
            gain = self.fine_gain
        elif magnitude < 0.025:
            gain = self.normal_gain
        else:
            gain = self.fast_gain

        screen_dx = dx / max(x_span, 1e-6)
        if mirror_x:
            screen_dx = -screen_dx
        screen_dy = dy / max(y_span, 1e-6)
        px = self._clamp(self._pointer[0] + screen_dx * gain)
        py = self._clamp(self._pointer[1] + screen_dy * gain)
        self._pointer = (px, py)
        return self._pointer

    def reset(self) -> None:
        self._anchor = None
        self._pointer = None
