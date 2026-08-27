from __future__ import annotations

import argparse
import time
from pathlib import Path

from pangu.gestures import MediaPipeHandTracker
from pangu.spatial_calibration import derive_axis_calibration


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate PANGU hand-pointer mapping")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--seconds-per-pose", type=float, default=2.2)
    parser.add_argument("--countdown", type=float, default=1.5)
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args()


def collect_pose(
    tracker: MediaPipeHandTracker,
    label: str,
    instruction: str,
    *,
    countdown: float,
    seconds: float,
    retries: int,
) -> list[tuple[float, float]]:
    for attempt in range(1, retries + 1):
        print()
        print(f"POSE: {label}")
        print(instruction)
        print("IMPORTANT: keep your WHOLE hand inside the webcam view.")
        print("Do not physically reach toward the screen edge; move the hand only inside the camera view.")
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
        if len(samples) >= 5:
            return samples

        if attempt < retries:
            print("No stable hand was seen. Move the hand back toward the camera center and try again.")
            print("Use brighter lighting and keep the palm facing the webcam.")

    raise RuntimeError(
        f"not enough hand samples for {label}; keep the whole hand inside the camera frame"
    )


def main() -> int:
    args = parse_args()
    if args.retries < 1:
        raise SystemExit("--retries must be at least 1")

    root = Path(__file__).resolve().parents[1]
    model = root / "models" / "vision" / "hand_landmarker.task"
    output = root / "runtime-data" / "spatial" / "pointer_calibration.json"

    tracker = MediaPipeHandTracker(model, camera_index=args.camera, max_hands=1)
    tracker.start()
    if tracker.diagnostics().get("status") != "READY":
        raise SystemExit(f"gesture tracker unavailable: {tracker.diagnostics()}")

    print("PANGU CAMERA-VISIBLE POINTER CALIBRATION")
    print("Keep one hand fully visible to the webcam for the entire calibration.")
    print("Use only your index finger as the pointer and keep the other fingers relaxed.")
    print("You are calibrating a comfortable AIR rectangle, not touching physical screen corners.")

    try:
        left = collect_pose(
            tracker,
            "LEFT LIMIT",
            "Move your pointing hand to the left-most comfortable position that is STILL clearly visible.",
            countdown=args.countdown,
            seconds=args.seconds_per_pose,
            retries=args.retries,
        )
        right = collect_pose(
            tracker,
            "RIGHT LIMIT",
            "Move your pointing hand to the right-most comfortable position that is STILL clearly visible.",
            countdown=args.countdown,
            seconds=args.seconds_per_pose,
            retries=args.retries,
        )
        top = collect_pose(
            tracker,
            "TOP LIMIT",
            "Move your pointing hand upward as far as comfortable while keeping the whole hand visible.",
            countdown=args.countdown,
            seconds=args.seconds_per_pose,
            retries=args.retries,
        )
        bottom = collect_pose(
            tracker,
            "BOTTOM LIMIT",
            "Move your pointing hand downward as far as comfortable while keeping the whole hand visible.",
            countdown=args.countdown,
            seconds=args.seconds_per_pose,
            retries=args.retries,
        )
    finally:
        tracker.stop()

    calibration = derive_axis_calibration(left, right, top, bottom)
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
