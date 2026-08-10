from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ScreenFrame:
    width: int
    height: int
    pixels_rgb: bytes
    captured_at: float
    frame_hash: str


@dataclass(frozen=True)
class VisualRegion:
    region_id: str
    label: str
    x: int
    y: int
    width: int
    height: int
    confidence: float
    source: str


@dataclass(frozen=True)
class OcrTextRegion:
    text: str
    x: int
    y: int
    width: int
    height: int
    confidence: float


@dataclass(frozen=True)
class TrackedVisualTarget:
    target_id: str
    label: str
    bounds: tuple[int, int, int, int]
    confidence: float
    age_frames: int


class ScreenCaptureProvider(Protocol):
    def capture(self) -> ScreenFrame: ...


class OcrProvider(Protocol):
    def recognize(self, frame: ScreenFrame) -> tuple[OcrTextRegion, ...]: ...


class PillowScreenCaptureProvider:
    """On-demand screenshot capture; frames are returned in memory and never persisted."""

    def capture(self) -> ScreenFrame:
        try:
            from PIL import ImageGrab
        except ImportError as error:
            raise RuntimeError("PILLOW_SCREEN_CAPTURE_UNAVAILABLE") from error
        image = ImageGrab.grab(all_screens=True).convert("RGB")
        payload = image.tobytes()
        digest = hashlib.sha256(payload).hexdigest()
        return ScreenFrame(image.width, image.height, payload, time.monotonic(), digest)


class NullOcrProvider:
    def recognize(self, frame: ScreenFrame) -> tuple[OcrTextRegion, ...]:
        return ()


class TemporalVisualTracker:
    """Track visual/UI targets across frames using label and IoU continuity."""

    def __init__(self, *, iou_threshold: float = 0.35, max_missing_frames: int = 4) -> None:
        if not 0.05 <= iou_threshold <= 0.95:
            raise ValueError("iou_threshold must be between 0.05 and 0.95")
        if not 0 <= max_missing_frames <= 30:
            raise ValueError("max_missing_frames must be between 0 and 30")
        self.iou_threshold = iou_threshold
        self.max_missing_frames = max_missing_frames
        self._tracks: dict[str, TrackedVisualTarget] = {}
        self._missing: dict[str, int] = {}
        self._counter = 0

    @staticmethod
    def _iou(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
        lx, ly, lw, lh = left
        rx, ry, rw, rh = right
        x1 = max(lx, rx)
        y1 = max(ly, ry)
        x2 = min(lx + lw, rx + rw)
        y2 = min(ly + lh, ry + rh)
        intersection = max(0, x2 - x1) * max(0, y2 - y1)
        union = lw * lh + rw * rh - intersection
        return intersection / union if union > 0 else 0.0

    def update(self, regions: tuple[VisualRegion, ...]) -> tuple[TrackedVisualTarget, ...]:
        unmatched = set(self._tracks)
        updated: dict[str, TrackedVisualTarget] = {}
        for region in regions:
            bounds = (region.x, region.y, region.width, region.height)
            candidates = [
                track
                for track in self._tracks.values()
                if track.label.casefold() == region.label.casefold()
                and self._iou(track.bounds, bounds) >= self.iou_threshold
            ]
            if candidates:
                best = max(candidates, key=lambda item: self._iou(item.bounds, bounds))
                unmatched.discard(best.target_id)
                updated[best.target_id] = TrackedVisualTarget(
                    best.target_id,
                    region.label,
                    bounds,
                    max(region.confidence, best.confidence * 0.9),
                    best.age_frames + 1,
                )
            else:
                self._counter += 1
                target_id = f"visual:{self._counter}"
                updated[target_id] = TrackedVisualTarget(
                    target_id, region.label, bounds, region.confidence, 1
                )
        for target_id in unmatched:
            missing = self._missing.get(target_id, 0) + 1
            if missing <= self.max_missing_frames:
                updated[target_id] = self._tracks[target_id]
                self._missing[target_id] = missing
            else:
                self._missing.pop(target_id, None)
        for target_id in updated:
            if target_id not in unmatched:
                self._missing[target_id] = 0
        self._tracks = updated
        return tuple(updated.values())


class ScreenVisionRuntime:
    """Privacy-controlled screenshot/OCR/visual-target fusion boundary.

    Capture is strictly on-demand. The runtime keeps only the latest hash and target
    tracker state; raw screen bytes are not written to disk by this component.
    """

    def __init__(
        self,
        capture: ScreenCaptureProvider,
        ocr: OcrProvider | None = None,
        tracker: TemporalVisualTracker | None = None,
    ) -> None:
        self.capture_provider = capture
        self.ocr = ocr or NullOcrProvider()
        self.tracker = tracker or TemporalVisualTracker()
        self.last_frame_hash: str | None = None

    def snapshot(self) -> tuple[ScreenFrame, tuple[OcrTextRegion, ...]]:
        frame = self.capture_provider.capture()
        if frame.width <= 0 or frame.height <= 0:
            raise RuntimeError("INVALID_SCREEN_FRAME")
        self.last_frame_hash = frame.frame_hash
        return frame, self.ocr.recognize(frame)

    def track_regions(self, regions: tuple[VisualRegion, ...]) -> tuple[TrackedVisualTarget, ...]:
        return self.tracker.update(regions)

    @staticmethod
    def resolve_text_target(
        query: str,
        ocr_regions: tuple[OcrTextRegion, ...],
        *,
        minimum_confidence: float = 0.55,
    ) -> OcrTextRegion | None:
        wanted = {token for token in query.casefold().split() if len(token) > 1}
        ranked: list[tuple[float, OcrTextRegion]] = []
        for region in ocr_regions:
            if region.confidence < minimum_confidence:
                continue
            actual = set(region.text.casefold().split())
            if not actual or not wanted:
                continue
            score = len(wanted & actual) / len(wanted) * region.confidence
            ranked.append((score, region))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked or ranked[0][0] < 0.45:
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.05:
            return None
        return ranked[0][1]
