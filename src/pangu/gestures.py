from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum
from math import atan2, degrees, hypot
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from .events import EventBus, EventEnvelope


class GestureKind(StrEnum):
    NONE = "NONE"
    POINT = "POINT"
    PINCH = "PINCH"
    GRAB = "GRAB"
    OPEN_PALM = "OPEN_PALM"
    SWIPE_LEFT = "SWIPE_LEFT"
    SWIPE_RIGHT = "SWIPE_RIGHT"
    SWIPE_UP = "SWIPE_UP"
    SWIPE_DOWN = "SWIPE_DOWN"
    TWO_HAND_SCALE_IN = "TWO_HAND_SCALE_IN"
    TWO_HAND_SCALE_OUT = "TWO_HAND_SCALE_OUT"
    TWO_HAND_ROTATE_CLOCKWISE = "TWO_HAND_ROTATE_CLOCKWISE"
    TWO_HAND_ROTATE_COUNTERCLOCKWISE = "TWO_HAND_ROTATE_COUNTERCLOCKWISE"


@dataclass(frozen=True)
class HandLandmark:
    x: float
    y: float
    z: float = 0.0


@dataclass(frozen=True)
class HandObservation:
    hand_id: str
    handedness: str
    landmarks: tuple[HandLandmark, ...]
    timestamp: float
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if len(self.landmarks) != 21:
            raise ValueError("a hand observation must contain exactly 21 landmarks")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("hand confidence must be between 0 and 1")


@dataclass(frozen=True)
class GestureDetection:
    gesture: GestureKind
    confidence: float
    hand_ids: tuple[str, ...]
    timestamp: float
    metadata: dict[str, float | str]


@dataclass(frozen=True)
class GestureConfiguration:
    pinch_distance: float = 0.055
    grab_radius: float = 0.23
    swipe_distance: float = 0.18
    swipe_window_seconds: float = 0.55
    two_hand_scale_delta: float = 0.12
    two_hand_rotation_degrees: float = 18.0
    history_seconds: float = 1.0
    minimum_confidence: float = 0.55


class HandTracker(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def read(self) -> tuple[HandObservation, ...]: ...
    def diagnostics(self) -> dict[str, object]: ...


def _distance(a: HandLandmark, b: HandLandmark) -> float:
    return hypot(a.x - b.x, a.y - b.y)


def _angle(a: HandLandmark, b: HandLandmark) -> float:
    return degrees(atan2(b.y - a.y, b.x - a.x))


def _angle_delta(current: float, previous: float) -> float:
    value = (current - previous + 180.0) % 360.0 - 180.0
    return value


class TemporalGestureRecognizer:
    """Deterministic static + temporal recognizer over 21-point hand landmarks."""

    def __init__(self, config: GestureConfiguration | None = None) -> None:
        self.config = config or GestureConfiguration()
        self._history: dict[str, deque[HandObservation]] = defaultdict(deque)
        self._pair_history: deque[tuple[float, float, float]] = deque()

    def _remember(self, observation: HandObservation) -> None:
        history = self._history[observation.hand_id]
        history.append(observation)
        cutoff = observation.timestamp - self.config.history_seconds
        while history and history[0].timestamp < cutoff:
            history.popleft()

    @staticmethod
    def _finger_extended(landmarks: tuple[HandLandmark, ...], tip: int, pip: int) -> bool:
        return landmarks[tip].y < landmarks[pip].y

    def _static_gesture(self, hand: HandObservation) -> GestureDetection:
        points = hand.landmarks
        thumb_tip, index_tip = points[4], points[8]
        pinch = _distance(thumb_tip, index_tip)
        if pinch <= self.config.pinch_distance:
            confidence = max(0.0, 1.0 - pinch / self.config.pinch_distance)
            return GestureDetection(
                GestureKind.PINCH,
                max(hand.confidence, confidence),
                (hand.hand_id,),
                hand.timestamp,
                {"pinch_distance": pinch},
            )

        extended = (
            self._finger_extended(points, 8, 6),
            self._finger_extended(points, 12, 10),
            self._finger_extended(points, 16, 14),
            self._finger_extended(points, 20, 18),
        )
        if extended == (True, False, False, False):
            return GestureDetection(
                GestureKind.POINT,
                hand.confidence,
                (hand.hand_id,),
                hand.timestamp,
                {"x": points[8].x, "y": points[8].y},
            )
        if all(extended):
            return GestureDetection(
                GestureKind.OPEN_PALM,
                hand.confidence,
                (hand.hand_id,),
                hand.timestamp,
                {},
            )

        wrist = points[0]
        mean_tip_radius = sum(_distance(wrist, points[index]) for index in (4, 8, 12, 16, 20)) / 5
        if mean_tip_radius <= self.config.grab_radius:
            return GestureDetection(
                GestureKind.GRAB,
                hand.confidence,
                (hand.hand_id,),
                hand.timestamp,
                {"mean_tip_radius": mean_tip_radius},
            )
        return GestureDetection(GestureKind.NONE, 0.0, (hand.hand_id,), hand.timestamp, {})

    def _swipe(self, hand: HandObservation) -> GestureDetection | None:
        history = self._history[hand.hand_id]
        if len(history) < 2:
            return None
        latest = history[-1]
        earliest = latest
        for candidate in history:
            if latest.timestamp - candidate.timestamp <= self.config.swipe_window_seconds:
                earliest = candidate
                break
        elapsed = latest.timestamp - earliest.timestamp
        if elapsed <= 0.0:
            return None
        dx = latest.landmarks[0].x - earliest.landmarks[0].x
        dy = latest.landmarks[0].y - earliest.landmarks[0].y
        if max(abs(dx), abs(dy)) < self.config.swipe_distance:
            return None
        if abs(dx) >= abs(dy):
            kind = GestureKind.SWIPE_RIGHT if dx > 0 else GestureKind.SWIPE_LEFT
            displacement = abs(dx)
        else:
            kind = GestureKind.SWIPE_DOWN if dy > 0 else GestureKind.SWIPE_UP
            displacement = abs(dy)
        return GestureDetection(
            kind,
            min(1.0, displacement / max(self.config.swipe_distance, 1e-6)),
            (hand.hand_id,),
            latest.timestamp,
            {"dx": dx, "dy": dy, "duration_seconds": elapsed},
        )

    def _two_hand(self, hands: tuple[HandObservation, ...]) -> GestureDetection | None:
        if len(hands) != 2:
            self._pair_history.clear()
            return None
        first, second = sorted(hands, key=lambda item: item.hand_id)
        timestamp = max(first.timestamp, second.timestamp)
        distance = _distance(first.landmarks[0], second.landmarks[0])
        angle = _angle(first.landmarks[0], second.landmarks[0])
        self._pair_history.append((timestamp, distance, angle))
        cutoff = timestamp - self.config.history_seconds
        while self._pair_history and self._pair_history[0][0] < cutoff:
            self._pair_history.popleft()
        if len(self._pair_history) < 2:
            return None
        _, initial_distance, initial_angle = self._pair_history[0]
        scale_delta = distance - initial_distance
        rotation_delta = _angle_delta(angle, initial_angle)
        ids = (first.hand_id, second.hand_id)
        confidence = min(first.confidence, second.confidence)
        if abs(scale_delta) >= self.config.two_hand_scale_delta:
            return GestureDetection(
                GestureKind.TWO_HAND_SCALE_OUT
                if scale_delta > 0
                else GestureKind.TWO_HAND_SCALE_IN,
                confidence,
                ids,
                timestamp,
                {"scale_delta": scale_delta, "distance": distance},
            )
        if abs(rotation_delta) >= self.config.two_hand_rotation_degrees:
            return GestureDetection(
                GestureKind.TWO_HAND_ROTATE_CLOCKWISE
                if rotation_delta > 0
                else GestureKind.TWO_HAND_ROTATE_COUNTERCLOCKWISE,
                confidence,
                ids,
                timestamp,
                {"rotation_degrees": rotation_delta},
            )
        return None

    def recognize(self, hands: tuple[HandObservation, ...]) -> tuple[GestureDetection, ...]:
        eligible = tuple(hand for hand in hands if hand.confidence >= self.config.minimum_confidence)
        for hand in eligible:
            self._remember(hand)
        pair = self._two_hand(eligible)
        if pair is not None:
            return (pair,)
        detections: list[GestureDetection] = []
        for hand in eligible:
            temporal = self._swipe(hand)
            detections.append(temporal or self._static_gesture(hand))
        return tuple(item for item in detections if item.gesture != GestureKind.NONE)


class MediaPipeHandTracker:
    """Optional on-device MediaPipe Tasks + OpenCV camera adapter.

    It never downloads a model and never stores camera frames. A missing model or
    backend produces an explicit unavailable state instead of a fake observation.
    """

    def __init__(self, model_path: Path, camera_index: int = 0, max_hands: int = 2) -> None:
        self.model_path = model_path.resolve()
        self.camera_index = camera_index
        self.max_hands = max_hands
        self._camera: Any | None = None
        self._landmarker: Any | None = None
        self._mp: Any | None = None
        self._cv2: Any | None = None
        self._status = "STOPPED"
        self._last_error: str | None = None

    def start(self) -> None:
        if not self.model_path.is_file():
            self._status = "MODEL_MISSING"
            self._last_error = "HAND_LANDMARKER_MODEL_UNAVAILABLE"
            return
        try:
            self._cv2 = __import__("cv2")
            self._mp = __import__("mediapipe")
            base_options = self._mp.tasks.BaseOptions(model_asset_path=str(self.model_path))
            options = self._mp.tasks.vision.HandLandmarkerOptions(
                base_options=base_options,
                running_mode=self._mp.tasks.vision.RunningMode.IMAGE,
                num_hands=self.max_hands,
                min_hand_detection_confidence=0.5,
                min_hand_presence_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            self._landmarker = self._mp.tasks.vision.HandLandmarker.create_from_options(options)
            self._camera = self._cv2.VideoCapture(self.camera_index)
            if not self._camera.isOpened():
                raise RuntimeError("camera unavailable")
            self._status = "READY"
            self._last_error = None
        except (ImportError, ModuleNotFoundError):
            self._status = "BACKEND_UNAVAILABLE"
            self._last_error = "GESTURE_BACKEND_UNAVAILABLE"
        except (OSError, RuntimeError, ValueError, AttributeError):
            self.stop()
            self._status = "FAILED"
            self._last_error = "GESTURE_START_FAILED"

    def read(self) -> tuple[HandObservation, ...]:
        if self._status != "READY" or self._camera is None or self._landmarker is None:
            return ()
        ok, frame = self._camera.read()
        if not ok:
            self._last_error = "CAMERA_FRAME_UNAVAILABLE"
            return ()
        rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        try:
            result = self._landmarker.detect(image)
        except (RuntimeError, ValueError):
            self._last_error = "HAND_LANDMARK_INFERENCE_FAILED"
            return ()
        timestamp = monotonic()
        observations: list[HandObservation] = []
        for index, points in enumerate(result.hand_landmarks):
            category = result.handedness[index][0] if index < len(result.handedness) else None
            handedness = str(getattr(category, "category_name", "unknown") or "unknown")
            confidence = float(getattr(category, "score", 1.0) or 0.0)
            observations.append(
                HandObservation(
                    f"{handedness.casefold()}-{index}",
                    handedness,
                    tuple(HandLandmark(float(p.x), float(p.y), float(p.z)) for p in points),
                    timestamp,
                    confidence,
                )
            )
        return tuple(observations)

    def stop(self) -> None:
        if self._camera is not None:
            self._camera.release()
        if self._landmarker is not None:
            self._landmarker.close()
        self._camera = None
        self._landmarker = None
        if self._status == "READY":
            self._status = "STOPPED"

    def diagnostics(self) -> dict[str, object]:
        return {
            "provider": "mediapipe-hand-landmarker",
            "status": self._status,
            "camera_index": self.camera_index,
            "max_hands": self.max_hands,
            "model_path_sanitized": self.model_path.name,
            "last_error": self._last_error,
            "frames_persisted": False,
        }


class GestureRuntime:
    """EventBus-integrated gesture perception loop."""

    def __init__(
        self,
        tracker: HandTracker,
        recognizer: TemporalGestureRecognizer,
        events: EventBus,
        poll_interval_seconds: float = 1 / 30,
    ) -> None:
        self.tracker = tracker
        self.recognizer = recognizer
        self.events = events
        self.poll_interval_seconds = poll_interval_seconds
        self._running = False

    async def start(self) -> None:
        self.tracker.start()
        self._running = self.tracker.diagnostics().get("status") == "READY"
        await self.events.publish(
            EventEnvelope("gesture.runtime.started", {"diagnostics": self.tracker.diagnostics()})
        )

    async def stop(self) -> None:
        self._running = False
        self.tracker.stop()
        await self.events.publish(EventEnvelope("gesture.runtime.stopped", {}))

    async def poll_once(self) -> tuple[GestureDetection, ...]:
        observations = self.tracker.read()
        detections = self.recognizer.recognize(observations)
        for detection in detections:
            await self.events.publish(
                EventEnvelope(
                    "gesture.detected",
                    {
                        "gesture": detection.gesture,
                        "confidence": detection.confidence,
                        "hand_ids": detection.hand_ids,
                        "timestamp": detection.timestamp,
                        "metadata": detection.metadata,
                    },
                )
            )
        return detections

    async def run(self) -> None:
        while self._running:
            await self.poll_once()
            await asyncio.sleep(self.poll_interval_seconds)
