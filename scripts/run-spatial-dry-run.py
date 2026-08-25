from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from pathlib import Path

from pangu.spatial_calibration import PointerCalibration
from pangu.spatial_live import LiveSpatialDryRunRuntime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PANGU live gesture + Chrome HUD in dry-run mode")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--no-mirror-x", action="store_true")
    parser.add_argument("--x-min", type=float)
    parser.add_argument("--x-max", type=float)
    parser.add_argument("--y-min", type=float)
    parser.add_argument("--y-max", type=float)
    parser.add_argument("--smoothing", type=float)
    parser.add_argument("--target-padding", type=float, default=0.04)
    return parser.parse_args()


def resolve_calibration(args: argparse.Namespace, root: Path) -> PointerCalibration:
    path = root / "runtime-data" / "spatial" / "pointer_calibration.json"
    if path.is_file():
        calibration = PointerCalibration.load(path)
    else:
        calibration = PointerCalibration(0.12, 0.88, 0.12, 0.88, True, 0.58)

    return PointerCalibration(
        x_min=calibration.x_min if args.x_min is None else args.x_min,
        x_max=calibration.x_max if args.x_max is None else args.x_max,
        y_min=calibration.y_min if args.y_min is None else args.y_min,
        y_max=calibration.y_max if args.y_max is None else args.y_max,
        mirror_x=False if args.no_mirror_x else calibration.mirror_x,
        smoothing=calibration.smoothing if args.smoothing is None else args.smoothing,
    )


async def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    model = root / "models" / "vision" / "hand_landmarker.task"
    state = root / "runtime-data" / "overlay" / "state.json"
    overlay = (
        root
        / "apps"
        / "overlay-host"
        / "bin"
        / "Release"
        / "net10.0-windows"
        / "Pangu.OverlayHost.dll"
    )
    calibration = resolve_calibration(args, root)

    if not model.is_file():
        raise SystemExit(f"hand model missing: {model}")
    if not overlay.is_file():
        raise SystemExit(
            "overlay build missing; run dotnet build apps/overlay-host/Pangu.OverlayHost.csproj -c Release"
        )

    state.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PANGU_HUD_STATE_FILE"] = str(state)
    env["PANGU_OVERLAY_INTERACTIVE"] = "false"
    overlay_process = subprocess.Popen(
        ["dotnet", str(overlay)],
        cwd=str(root),
        env=env,
    )

    runtime = LiveSpatialDryRunRuntime(
        model_path=model,
        hud_state_path=state,
        camera_index=args.camera,
        mirror_x=calibration.mirror_x,
        pointer_x_min=calibration.x_min,
        pointer_x_max=calibration.x_max,
        pointer_y_min=calibration.y_min,
        pointer_y_max=calibration.y_max,
        pointer_smoothing=calibration.smoothing,
        target_padding=args.target_padding,
    )

    try:
        await runtime.start()
        print("PANGU LIVE SPATIAL DRY RUN")
        print("- Chrome must be foreground")
        print("- closed fist can AIR-GRAB the active Chrome tab; no pointing/selecting required")
        print("- while grabbed, move the closed fist to build throw velocity")
        print("- open the palm after a fast throw to produce a close proposal")
        print("- pointer/hover remains optional for choosing a non-active target")
        print("- real tab closing is DISABLED in this validation run")
        print(
            f"- pointer calibration x=({calibration.x_min:.3f},{calibration.x_max:.3f}) "
            f"y=({calibration.y_min:.3f},{calibration.y_max:.3f}) "
            f"mirror_x={calibration.mirror_x} smoothing={calibration.smoothing:.2f}"
        )
        print()
        print("START:", runtime.diagnostics())
        await runtime.run(args.seconds)
        print("FINAL:", runtime.diagnostics())
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
