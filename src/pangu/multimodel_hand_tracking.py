from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from importlib import import_module
from math import hypot
from pathlib import Path
from time import monotonic
from typing import Any

from .gestures import HandLandmark, HandObservation


@dataclass(frozen=True)
class MultiModelDiagnostics:
    status: str
    primary_model: str
    secondary_model: str
    secondary_status: str
    fused_frames: int
    primary_only_frames: int
    secondary_only_frames: int
    predicted_frames: int
    last_error: str | None


@dataclass(frozen=True)
class MotionSample:
    timestamp: float
    wrist_x: float
    wrist_y: float


class HandTrajectoryPredictor:
    """Short-horizon constant-acceleration hand continuation with confidence decay.

    This does not claim to see outside the camera field of view. It predicts the hand
    for a bounded interval from the last measured direction, distance and speed so the
    spatial pointer does not instantly collapse when the hand briefly leaves frame.
    """

    def __init__(self, *, horizon_seconds: float = 0.45, damping: float = 0.82) -> None:
        if not 0.10 <= horizon_seconds <= 1.0:
            raise ValueError("horizon_seconds must be between 0.10 and 1.0")
        if not 0.0 < damping <= 1.0:
            raise ValueError("damping must be in (0, 1]")
        self.horizon_seconds = horizon_seconds
        self.damping = damping
        self._history: deque[tuple[float, HandObservation]] = deque(maxlen=8)

    def observe(self, hand: HandObservation) -> None:
        self._history.append((hand.timestamp, hand))

    @staticmethod
    def _velocity(
        older: tuple[float, HandObservation], newer: tuple[float, HandObservation]
    ) -> tuple[float, float, float]:
        dt = max(1e-4, newer[0] - older[0])
        old_wrist = older[1].landmarks[0]
        new_wrist = newer[1].landmarks[0]
        vx = (new_wrist.x - old_wrist.x) / dt
        vy = (new_wrist.y - old_wrist.y) / dt
        return vx, vy, hypot(vx, vy)

    def predict(self, now: float | None = None) -> HandObservation | None:
        if len(self._history) < 2:
            return None
        current_time = monotonic() if now is None else now
        last_time, last = self._history[-1]
        elapsed = current_time - last_time
        if elapsed <= 0.0 or elapsed > self.horizon_seconds:
            return None

        vx, vy, speed = self._velocity(self._history[-2], self._history[-1])
        if speed < 0.015:
            return None

        ax = 0.0
        ay = 0.0
        if len(self._history) >= 3:
            pvx, pvy, _ = self._velocity(self._history[-3], self._history[-2])
            dt = max(1e-4, self._history[-1][0] - self._history[-2][0])
            ax = (vx - pvx) / dt
            ay = (vy - pvy) / dt

        decay = max(0.0, 1.0 - elapsed / self.horizon_seconds)
        gain = self.damping * elapsed
        offset_x = vx * gain + 0.5 * ax * elapsed * elapsed * 0.35
        offset_y = vy * gain + 0.5 * ay * elapsed * elapsed * 0.35
        # Bound extrapolation so a brief occlusion cannot teleport the pointer.
        distance = hypot(offset_x, offset_y)
        max_distance = 0.28
        if distance > max_distance:
            scale = max_distance / max(distance, 1e-6)
            offset_x *= scale
            offset_y *= scale

        landmarks = tuple(
            HandLandmark(
                min(1.15, max(-0.15, point.x + offset_x)),
                min(1.15, max(-0.15, point.y + offset_y)),
                point.z,
            )
            for point in last.landmarks
        )
        return HandObservation(
            last.hand_id,
            last.handedness,
            landmarks,
            current_time,
            max(0.10, last.confidence * decay * 0.72),
        )


class LandmarkFusion:
    """Confidence-weighted 21-keypoint fusion for MediaPipe + YOLO hand pose."""

    @staticmethod
    def palm_center(hand: HandObservation) -> tuple[float, float]:
        ids = (0, 5, 9, 13, 17)
        return (
            sum(hand.landmarks[index].x for index in ids) / len(ids),
            sum(hand.landmarks[index].y for index in ids) / len(ids),
        )

    @classmethod
    def match(
        cls,
        primary: tuple[HandObservation, ...],
        secondary: tuple[HandObservation, ...],
        *,
        maximum_distance: float = 0.22,
    ) -> tuple[tuple[HandObservation, HandObservation], ...]:
        remaining = list(secondary)
        pairs: list[tuple[HandObservation, HandObservation]] = []
        for first in primary:
            fx, fy = cls.palm_center(first)
            best: tuple[float, HandObservation] | None = None
            for second in remaining:
                sx, sy = cls.palm_center(second)
                distance = hypot(fx - sx, fy - sy)
                if distance <= maximum_distance and (best is None or distance < best[0]):
                    best = (distance, second)
            if best is not None:
                pairs.append((first, best[1]))
                remaining.remove(best[1])
        return tuple(pairs)

    @staticmethod
    def fuse(primary: HandObservation, secondary: HandObservation) -> HandObservation:
        pw = max(0.05, primary.confidence)
        sw = max(0.05, secondary.confidence)
        total = pw + sw
        points = tuple(
            HandLandmark(
                (a.x * pw + b.x * sw) / total,
                (a.y * pw + b.y * sw) / total,
                (a.z * pw + b.z * sw) / total,
            )
            for a, b in zip(primary.landmarks, secondary.landmarks, strict=True)
        )
        agreement = max(
            0.0,
            1.0
            - hypot(
                primary.landmarks[0].x - secondary.landmarks[0].x,
                primary.landmarks[0].y - secondary.landmarks[0].y,
            )
            / 0.22,
        )
        confidence = min(1.0, 0.58 * primary.confidence + 0.32 * secondary.confidence + 0.10 * agreement)
        return HandObservation(
            primary.hand_id,
            primary.handedness,
            points,
            max(primary.timestamp, secondary.timestamp),
            confidence,
        )


class MultiModelHandTracker:
    """Shared-camera MediaPipe + optional YOLO 21-keypoint hand-pose fusion tracker.

    One camera frame is fed to both models, then their landmark estimates are matched
    and confidence-weighted. If the YOLO checkpoint is absent, the tracker explicitly
    reports DEGRADED_PRIMARY_ONLY rather than pretending two-model fusion is active.
    """

    def __init__(
        self,
        *,
        mediapipe_model_path: Path,
        yolo_model_path: Path,
        camera_index: int = 0,
        max_hands: int = 2,
        prediction_horizon_seconds: float = 0.45,
    ) -> None:
        self.mediapipe_model_path = mediapipe_model_path.resolve()
        self.yolo_model_path = yolo_model_path.resolve()
        self.camera_index = camera_index
        self.max_hands = max_hands
        self.predictor = HandTrajectoryPredictor(horizon_seconds=prediction_horizon_seconds)
        self._camera: Any | None = None
        self._cv2: Any | None = None
        self._mp: Any | None = None
        self._landmarker: Any | None = None
        self._yolo: Any | None = None
        self._started_at: float | None = None
        self._last_timestamp_ms = -1
        self._status = "STOPPED"
        self._secondary_status = "NOT_STARTED"
        self._last_error: str | None = None
        self._fused_frames = 0
        self._primary_only_frames = 0
        self._secondary_only_frames = 0
        self._predicted_frames = 0

    def start(self) -> None:
        if not self.mediapipe_model_path.is_file():
            self._status = "MODEL_MISSING"
            self._last_error = "MEDIAPIPE_HAND_MODEL_MISSING"
            return
        try:
            self._cv2 = import_module("cv2")
            self._mp = import_module("mediapipe")
            options = self._mp.tasks.vision.HandLandmarkerOptions(
                base_options=self._mp.tasks.BaseOptions(
                    model_asset_path=str(self.mediapipe_model_path)
                ),
                running_mode=self._mp.tasks.vision.RunningMode.VIDEO,
                num_hands=self.max_hands,
                min_hand_detection_confidence=0.25,
                min_hand_presence_confidence=0.25,
                min_tracking_confidence=0.25,
            )
            self._landmarker = self._mp.tasks.vision.HandLandmarker.create_from_options(options)
            self._camera = self._cv2.VideoCapture(self.camera_index)
            if not self._camera.isOpened():
                raise RuntimeError("camera unavailable")

            if self.yolo_model_path.is_file():
                try:
                    ultralytics = import_module("ultralytics")
                    self._yolo = ultralytics.YOLO(str(self.yolo_model_path))
                    self._secondary_status = "READY"
                except (ImportError, ModuleNotFoundError):
                    self._secondary_status = "BACKEND_UNAVAILABLE"
                except (OSError, RuntimeError, ValueError):
                    self._secondary_status = "MODEL_LOAD_FAILED"
            else:
                self._secondary_status = "MODEL_MISSING"

            self._started_at = monotonic()
            self._last_timestamp_ms = -1
            self._status = (
                "READY_FUSED" if self._secondary_status == "READY" else "DEGRADED_PRIMARY_ONLY"
            )
            self._last_error = None
        except (ImportError, ModuleNotFoundError):
            self._status = "BACKEND_UNAVAILABLE"
            self._last_error = "MULTIMODEL_VISION_BACKEND_UNAVAILABLE"
        except (OSError, RuntimeError, ValueError, AttributeError):
            self.stop()
            self._status = "FAILED"
            self._last_error = "MULTIMODEL_TRACKER_START_FAILED"

    def _mediapipe_observations(self, frame: Any, timestamp: float) -> tuple[HandObservation, ...]:
        if self._landmarker is None or self._mp is None or self._cv2 is None or self._started_at is None:
            return ()
        rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        timestamp_ms = max(
            self._last_timestamp_ms + 1,
            int((timestamp - self._started_at) * 1000),
        )
        self._last_timestamp_ms = timestamp_ms
        try:
            result = self._landmarker.detect_for_video(image, timestamp_ms)
        except (RuntimeError, ValueError):
            return ()
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

    def _yolo_observations(self, frame: Any, timestamp: float) -> tuple[HandObservation, ...]:
        if self._yolo is None:
            return ()
        try:
            results = self._yolo.predict(
                source=frame,
                imgsz=640,
                conf=0.20,
                verbose=False,
                max_det=self.max_hands,
            )
        except (RuntimeError, ValueError, OSError):
            self._secondary_status = "INFERENCE_FAILED"
            return ()
        if not results:
            return ()
        result = results[0]
        keypoints = getattr(result, "keypoints", None)
        if keypoints is None or getattr(keypoints, "xyn", None) is None:
            return ()
        xyn = keypoints.xyn
        conf_tensor = getattr(keypoints, "conf", None)
        box_conf = getattr(getattr(result, "boxes", None), "conf", None)
        observations: list[HandObservation] = []
        count = int(xyn.shape[0]) if hasattr(xyn, "shape") else 0
        for index in range(min(count, self.max_hands)):
            row = xyn[index]
            if len(row) != 21:
                continue
            points = tuple(
                HandLandmark(float(point[0]), float(point[1]), 0.0) for point in row
            )
            confidence = 0.75
            try:
                if conf_tensor is not None:
                    confidence = float(conf_tensor[index].mean())
                elif box_conf is not None:
                    confidence = float(box_conf[index])
            except (TypeError, ValueError, IndexError, AttributeError):
                confidence = 0.75
            observations.append(
                HandObservation(
                    f"yolo-{index}",
                    "Unknown",
                    points,
                    timestamp,
                    min(1.0, max(0.0, confidence)),
                )
            )
        return tuple(observations)

    def read(self) -> tuple[HandObservation, ...]:
        if self._status not in {"READY_FUSED", "DEGRADED_PRIMARY_ONLY"} or self._camera is None:
            return ()
        ok, frame = self._camera.read()
        now = monotonic()
        if not ok:
            predicted = self.predictor.predict(now)
            if predicted is not None:
                self._predicted_frames += 1
                return (predicted,)
            self._last_error = "CAMERA_FRAME_UNAVAILABLE"
            return ()

        primary = self._mediapipe_observations(frame, now)
        secondary = self._yolo_observations(frame, now) if self._yolo is not None else ()

        if primary and secondary:
            pairs = LandmarkFusion.match(primary, secondary)
            fused_by_id = {first.hand_id: LandmarkFusion.fuse(first, second) for first, second in pairs}
            output = tuple(fused_by_id.get(hand.hand_id, hand) for hand in primary)
            self._fused_frames += 1
        elif primary:
            output = primary
            self._primary_only_frames += 1
        elif secondary:
            output = secondary
            self._secondary_only_frames += 1
        else:
            predicted = self.predictor.predict(now)
            if predicted is not None:
                self._predicted_frames += 1
                return (predicted,)
            return ()

        if output:
            # The primary hand is enough for the short-horizon continuation state.
            self.predictor.observe(max(output, key=lambda item: item.confidence))
        return output

    def stop(self) -> None:
        if self._camera is not None:
            self._camera.release()
        if self._landmarker is not None:
            self._landmarker.close()
        self._camera = None
        self._landmarker = None
        self._yolo = None
        self._started_at = None
        self._last_timestamp_ms = -1
        if self._status in {"READY_FUSED", "DEGRADED_PRIMARY_ONLY"}:
            self._status = "STOPPED"

    def diagnostics(self) -> dict[str, object]:
        return {
            "provider": "mediapipe+yolo-hand-pose-fusion",
            "status": self._status,
            "camera_index": self.camera_index,
            "primary_model": self.mediapipe_model_path.name,
            "secondary_model": self.yolo_model_path.name,
            "secondary_status": self._secondary_status,
            "fused_frames": self._fused_frames,
            "primary_only_frames": self._primary_only_frames,
            "secondary_only_frames": self._secondary_only_frames,
            "predicted_frames": self._predicted_frames,
            "prediction_horizon_seconds": self.predictor.horizon_seconds,
            "last_error": self._last_error,
            "frames_persisted": False,
        }
