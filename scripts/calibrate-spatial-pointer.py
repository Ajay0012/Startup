from __future__ import annotations

import argparse
import time
from pathlib import Path

from pangu.gestures import MediaPipeHandTracker
from pangu.spatial_calibration import derive_pointer_calibration


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate PANGU hand-pointer mapping")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--seconds-per-corner", type=float, default=2.5)
    parser.add_argument("--countdown", type=float, default=2.0)
    return parser.parse_args()


def collect_corner(
    tracker: MediaPipeHandTracker,
    label: str,
    *,
    countdown: float,
    seconds: float,
) -> list[tuple[float, float]]:
    print()
    print(f"POINT at the {label} of your SCREEN using only your index finger.")
    print("Keep the rest of the hand relaxed and keep the fingertip visible to the camera.")
    print(f"Sampling starts in {countdown:.1f} seconds...")
    time.sleep(countdown)

    samples: list[tuple[float, float]] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        hands = tracker.read()
        if hands:
            hand = max(hands, key=lambda item: item.confidence)
            tip = hand.landmarks[8]
            samples.append((tip.x, tip.y))
        time.sleep(0.025)

    print(f"Captured {len(samples)} fingertip samples for {label}.")
    if len(samples) < 3:
        raise RuntimeError(f"not enough hand samples for {label}; improve lighting/camera framing")
    return samples


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    model = root / "models" / "vision" / "hand_landmarker.task"
    output = root / "runtime-data" / "spatial" / "pointer_calibration.json"

    tracker = MediaPipeHandTracker(model, camera_index=args.camera, max_hands=1)
    tracker.start()
    if tracker.diagnostics().get("status") != "READY":
        raise SystemExit(f"gesture tracker unavailable: {tracker.diagnostics()}")

    print("PANGU POINTER CALIBRATION")
    print("You will point to four SCREEN corners. Do not move the laptop/camera during calibration.")

    try:
        top_left = collect_corner(
            tracker,
            "TOP-LEFT corner",
            countdown=args.countdown,
            seconds=args.seconds_per_corner,
        )
        top_right = collect_corner(
            tracker,
            "TOP-RIGHT corner",
            countdown=args.countdown,
            seconds=args.seconds_per_corner,
        )
        bottom_left = collect_corner(
            tracker,
            "BOTTOM-LEFT corner",
            countdown=args.countdown,
            seconds=args.seconds_per_corner,
        )
        bottom_right = collect_corner(
            tracker,
            "BOTTOM-RIGHT corner",
            countdown=args.countdown,
            seconds=args.seconds_per_corner,
        )
    finally:
        tracker.stop()

    calibration = derive_pointer_calibration(
        top_left,
        top_right,
        bottom_left,
        bottom_right,
    )
    calibration.save(output)

    print()
    print("CALIBRATION SAVED:", output)
    print(
        "RESULT: "
        f"x=({calibration.x_min:.4f},{calibration.x_max:.4f}) "
        f"y=({calibration.y_min:.4f},{calibration.y_max:.4f}) "
        f"mirror_x={calibration.mirror_x} "
        f"smoothing={calibration.smoothing:.2f}"
    )
    print("The live dry-run launcher will use this calibration automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
