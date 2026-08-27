from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CameraFrame:
    width: int
    height: int
    pixels_bgr: bytes
    captured_at: float


@dataclass(frozen=True)
class VisualDetection:
    label: str
    confidence: float
    bounds: tuple[int, int, int, int]
    track_id: str | None = None


@dataclass(frozen=True)
class QrDetection:
    value: str
    points: tuple[tuple[float, float], ...]


class CameraProvider(Protocol):
    def start(self) -> None: ...
    def capture(self) -> CameraFrame: ...
    def stop(self) -> None: ...


class ObjectDetectionProvider(Protocol):
    def detect(self, frame: CameraFrame) -> tuple[VisualDetection, ...]: ...


class OpenCvCameraProvider:
    """On-device camera capture with no frame persistence."""

    def __init__(self, camera_index: int = 0) -> None:
        self.camera_index = camera_index
        self._capture: object | None = None

    def start(self) -> None:
        try:
            import cv2
        except ImportError as error:
            raise RuntimeError("OPENCV_CAMERA_UNAVAILABLE") from error
        capture = cv2.VideoCapture(self.camera_index)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError("CAMERA_OPEN_FAILED")
        self._capture = capture

    def capture(self) -> CameraFrame:
        if self._capture is None:
            raise RuntimeError("CAMERA_NOT_STARTED")
        ok, frame = self._capture.read()  # type: ignore[attr-defined]
        if not ok or frame is None:
            raise RuntimeError("CAMERA_FRAME_FAILED")
        height, width = frame.shape[:2]
        return CameraFrame(width, height, frame.tobytes(), time.monotonic())

    def stop(self) -> None:
        if self._capture is not None:
            self._capture.release()  # type: ignore[attr-defined]
            self._capture = None


class NullObjectDetector:
    def detect(self, frame: CameraFrame) -> tuple[VisualDetection, ...]:
        return ()


class CameraVisionRuntime:
    """Optional physical-world vision boundary.

    It supports explicit camera sessions, QR decoding and pluggable local object/person
    detectors. No frame is persisted by this runtime. Face/person recognition must be
    separately enabled and must not be inferred from generic object labels.
    """

    def __init__(
        self,
        camera: CameraProvider,
        detector: ObjectDetectionProvider | None = None,
    ) -> None:
        self.camera = camera
        self.detector = detector or NullObjectDetector()
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.camera.start()
        self.started = True

    def stop(self) -> None:
        if not self.started:
            return
        self.camera.stop()
        self.started = False

    def observe(self) -> tuple[CameraFrame, tuple[VisualDetection, ...], tuple[QrDetection, ...]]:
        if not self.started:
            raise RuntimeError("CAMERA_NOT_STARTED")
        frame = self.camera.capture()
        detections = self.detector.detect(frame)
        return frame, detections, self._decode_qr(frame)

    @staticmethod
    def _decode_qr(frame: CameraFrame) -> tuple[QrDetection, ...]:
        try:
            import cv2
            import numpy as np
        except ImportError:
            return ()
        image = np.frombuffer(frame.pixels_bgr, dtype=np.uint8).reshape(
            (frame.height, frame.width, 3)
        )
        detector = cv2.QRCodeDetector()
        try:
            ok, decoded, points, _ = detector.detectAndDecodeMulti(image)
        except cv2.error:
            return ()
        if not ok or points is None:
            return ()
        result: list[QrDetection] = []
        for value, polygon in zip(decoded, points, strict=False):
            if not value:
                continue
            result.append(
                QrDetection(
                    value,
                    tuple((float(point[0]), float(point[1])) for point in polygon),
                )
            )
        return tuple(result)
