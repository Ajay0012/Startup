from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from pathlib import Path

from pangu.spatial_calibration import PointerCalibration
from pangu.spatial_live_fused import FusedAdvancedLiveSpatialDryRunRuntime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PANGU fused multi-model hand pointing")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=90.0)
    parser.add_argument("--yolo-hand-model")
    parser.add_argument("--allow-primary-only", action="store_true")
    parser.add_argument("--prediction-horizon", type=float, default=0.45)
    parser.add_argument("--throw-threshold", type=float, default=0.22)
    return parser.parse_args()


def load_calibration(root: Path) -> PointerCalibration:
    path = root / "runtime-data" / "spatial" / "pointer_calibration.json"
    if path.is_file():
        return PointerCalibration.load(path)
    return PointerCalibration(0.12, 0.88, 0.12, 0.88, True, 0.58)


async def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    mediapipe_model = root / "models" / "vision" / "hand_landmarker.task"
    default_yolo = root / "models" / "vision" / "yolo26-hand-pose" / "train" / "weights" / "best.pt"
    yolo_model = Path(args.yolo_hand_model).expanduser().resolve() if args.yolo_hand_model else default_yolo
    state = root / "runtime-data" / "overlay" / "state.json"
    overlay = root / "apps" / "overlay-host" / "bin" / "Release" / "net10.0-windows" / "Pangu.OverlayHost.dll"

    if not mediapipe_model.is_file():
        raise SystemExit(f"MediaPipe hand model missing: {mediapipe_model}")
    if not yolo_model.is_file() and not args.allow_primary_only:
        raise SystemExit(
            "YOLO 21-keypoint hand model missing. True two-model fusion is not active.\n"
            f"Expected: {yolo_model}\n"
            "Install the advanced vision dependency and train/copy a dedicated 21-keypoint "
            "hand-pose checkpoint there, or pass --yolo-hand-model <path>.\n"
            "Use --allow-primary-only only for fallback testing."
        )
    if not overlay.is_file():
        raise SystemExit("overlay build missing; build Pangu.OverlayHost first")

    cal = load_calibration(root)
    state.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PANGU_HUD_STATE_FILE"] = str(state)
    env["PANGU_OVERLAY_INTERACTIVE"] = "false"
    overlay_process = subprocess.Popen(["dotnet", str(overlay)], cwd=str(root), env=env)

    runtime = FusedAdvancedLiveSpatialDryRunRuntime(
        model_path=mediapipe_model,
        yolo_model_path=yolo_model,
        hud_state_path=state,
        camera_index=args.camera,
        prediction_horizon_seconds=args.prediction_horizon,
        mirror_x=cal.mirror_x,
        pointer_x_min=cal.x_min,
        pointer_x_max=cal.x_max,
        pointer_y_min=cal.y_min,
        pointer_y_max=cal.y_max,
        pointer_smoothing=cal.smoothing,
        throw_velocity_threshold=args.throw_threshold,
    )

    try:
        await runtime.start()
        fusion = runtime.fusion_diagnostics()
        if not fusion.get("true_two_model_fusion") and not args.allow_primary_only:
            raise RuntimeError(f"secondary hand model did not start: {fusion}")

        print("PANGU FUSED MULTI-MODEL SPATIAL DRY RUN")
        print("- model 1: MediaPipe 21-point Hand Landmarker")
        print("- model 2: YOLO dedicated 21-keypoint hand-pose model")
        print("- confidence-weighted landmark fusion")
        print("- direction + distance + speed based intent prediction")
        print("- short bounded out-of-frame/occlusion trajectory continuation")
        print("- semantic target lock + precision drag")
        print("- real closing remains DISABLED")
        print("FUSION START:", fusion)
        print("START:", runtime.diagnostics())
        await runtime.run(args.seconds)
        print("FINAL:", runtime.diagnostics())
        print("FUSION:", runtime.fusion_diagnostics())
        print("PRECISION:", runtime.precision_diagnostics())
        print("THROW:", runtime.throw_diagnostics())
    finally:
        await runtime.stop()
        if overlay_process.poll() is None:
            overlay_process.terminate()
            try:
                overlay_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                overlay_process.kill()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
