from __future__ import annotations

from pathlib import Path

import numpy as np

from .camera_vision import CameraFrame, VisualDetection


class OpenCvYoloObjectDetector:
    """Local YOLO-style ONNX detector using OpenCV DNN.

    The model and class labels must be explicitly installed. No network download occurs
    during startup or inference. The adapter supports common YOLOv8/YOLO11 exported
    detection tensors shaped as [1,C,N] or [1,N,C].
    """

    def __init__(
        self,
        model_path: Path,
        labels_path: Path,
        *,
        input_size: int = 640,
        confidence_threshold: float = 0.35,
        nms_threshold: float = 0.45,
        max_detections: int = 100,
    ) -> None:
        self.model_path = model_path
        self.labels_path = labels_path
        self.input_size = input_size
        self.confidence_threshold = confidence_threshold
        self.nms_threshold = nms_threshold
        self.max_detections = max_detections
        self._net: object | None = None
        self._labels: tuple[str, ...] | None = None

    def _load(self) -> tuple[object, tuple[str, ...]]:
        if self._net is not None and self._labels is not None:
            return self._net, self._labels
        if not self.model_path.is_file() or not self.labels_path.is_file():
            raise RuntimeError("OBJECT_DETECTION_MODEL_UNAVAILABLE")
        try:
            import cv2
        except ImportError as error:
            raise RuntimeError("OPENCV_DNN_UNAVAILABLE") from error
        labels = tuple(
            line.strip()
            for line in self.labels_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if not labels:
            raise RuntimeError("OBJECT_DETECTION_LABELS_INVALID")
        try:
            net = cv2.dnn.readNetFromONNX(str(self.model_path))
        except cv2.error as error:
            raise RuntimeError("OBJECT_DETECTION_MODEL_LOAD_FAILED") from error
        self._net = net
        self._labels = labels
        return net, labels

    def detect(self, frame: CameraFrame) -> tuple[VisualDetection, ...]:
        try:
            import cv2
        except ImportError as error:
            raise RuntimeError("OPENCV_DNN_UNAVAILABLE") from error
        net, labels = self._load()
        image = np.frombuffer(frame.pixels_bgr, dtype=np.uint8).reshape(
            frame.height, frame.width, 3
        )
        scale = min(self.input_size / frame.width, self.input_size / frame.height)
        resized_width = max(1, int(round(frame.width * scale)))
        resized_height = max(1, int(round(frame.height * scale)))
        resized = cv2.resize(image, (resized_width, resized_height))
        canvas = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        x_pad = (self.input_size - resized_width) // 2
        y_pad = (self.input_size - resized_height) // 2
        canvas[
            y_pad : y_pad + resized_height,
            x_pad : x_pad + resized_width,
        ] = resized
        blob = cv2.dnn.blobFromImage(
            canvas,
            scalefactor=1.0 / 255.0,
            size=(self.input_size, self.input_size),
            swapRB=True,
            crop=False,
        )
        net.setInput(blob)  # type: ignore[attr-defined]
        raw = np.asarray(net.forward())  # type: ignore[attr-defined]
        if raw.ndim == 3:
            raw = raw[0]
        if raw.ndim != 2:
            raise RuntimeError("OBJECT_DETECTION_OUTPUT_UNSUPPORTED")
        expected_channels = len(labels) + 4
        if raw.shape[0] == expected_channels and raw.shape[1] != expected_channels:
            rows = raw.T
        elif raw.shape[1] >= expected_channels:
            rows = raw
        else:
            raise RuntimeError("OBJECT_DETECTION_OUTPUT_UNSUPPORTED")

        boxes: list[list[int]] = []
        scores: list[float] = []
        class_ids: list[int] = []
        for row in rows:
            class_scores = row[4 : 4 + len(labels)]
            if class_scores.size == 0:
                continue
            class_id = int(np.argmax(class_scores))
            confidence = float(class_scores[class_id])
            if confidence < self.confidence_threshold:
                continue
            cx, cy, width, height = (float(value) for value in row[:4])
            left = (cx - width / 2 - x_pad) / scale
            top = (cy - height / 2 - y_pad) / scale
            box_width = width / scale
            box_height = height / scale
            x = max(0, min(frame.width - 1, int(round(left))))
            y = max(0, min(frame.height - 1, int(round(top))))
            w = max(1, min(frame.width - x, int(round(box_width))))
            h = max(1, min(frame.height - y, int(round(box_height))))
            boxes.append([x, y, w, h])
            scores.append(confidence)
            class_ids.append(class_id)
        if not boxes:
            return ()
        indices = cv2.dnn.NMSBoxes(
            boxes,
            scores,
            self.confidence_threshold,
            self.nms_threshold,
        )
        flattened = np.asarray(indices).reshape(-1).tolist() if len(indices) else []
        detections: list[VisualDetection] = []
        for raw_index in flattened[: self.max_detections]:
            index = int(raw_index)
            x, y, width, height = boxes[index]
            detections.append(
                VisualDetection(
                    labels[class_ids[index]],
                    scores[index],
                    (x, y, width, height),
                )
            )
        return tuple(detections)
