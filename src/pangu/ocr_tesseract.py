from __future__ import annotations

import csv
import io
import shutil
import subprocess

from .screen_vision import OcrTextRegion, ScreenFrame


class TesseractOcrProvider:
    """Local OCR through the Tesseract CLI using stdin/stdout only.

    PANGU converts the in-memory RGB frame to PNG bytes and pipes those bytes directly
    to Tesseract. This provider does not create screenshot files on disk.
    """

    def __init__(
        self,
        executable: str | None = None,
        *,
        language: str = "eng",
        page_segmentation_mode: int = 6,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.executable = executable or shutil.which("tesseract")
        self.language = language
        self.page_segmentation_mode = page_segmentation_mode
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self.executable)

    def recognize(self, frame: ScreenFrame) -> tuple[OcrTextRegion, ...]:
        if not self.executable:
            raise RuntimeError("TESSERACT_OCR_UNAVAILABLE")
        try:
            from PIL import Image
        except ImportError as error:
            raise RuntimeError("PILLOW_OCR_UNAVAILABLE") from error
        image = Image.frombytes("RGB", (frame.width, frame.height), frame.pixels_rgb)
        encoded = io.BytesIO()
        image.save(encoded, format="PNG", optimize=False)
        try:
            completed = subprocess.run(
                [
                    self.executable,
                    "stdin",
                    "stdout",
                    "-l",
                    self.language,
                    "--psm",
                    str(self.page_segmentation_mode),
                    "tsv",
                ],
                input=encoded.getvalue(),
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("TESSERACT_OCR_FAILED") from error
        if completed.returncode != 0:
            raise RuntimeError("TESSERACT_OCR_FAILED")
        text = completed.stdout.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        regions: list[OcrTextRegion] = []
        for row in reader:
            value = " ".join((row.get("text") or "").split())
            if not value:
                continue
            try:
                confidence_raw = float(row.get("conf") or -1)
                left = int(row.get("left") or 0)
                top = int(row.get("top") or 0)
                width = int(row.get("width") or 0)
                height = int(row.get("height") or 0)
            except ValueError:
                continue
            if confidence_raw < 0 or width <= 0 or height <= 0:
                continue
            regions.append(
                OcrTextRegion(
                    value,
                    left,
                    top,
                    width,
                    height,
                    max(0.0, min(1.0, confidence_raw / 100.0)),
                )
            )
        return tuple(regions)
