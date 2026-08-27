from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from pathlib import Path

from pangu.spatial_calibration import PointerCalibration
from pangu.spatial_live_advanced import AdvancedLiveSpatialDryRunRuntime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PANGU advanced hand pointing + Chrome HUD")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--no-mirror-x", action="store_true")
    parser.add_argument("--x-min", type=float)
    parser.add_argument("--x-max", type=float)
    parser.add_argument("--y-min", type=float)
    parser.add_argument("--y-max", type=float)
    parser.add_argument("--smoothing", type=float)
    parser.add_argument("--ray-gain", type=float, default=0.22)
    parser.add_argument("--snap-radius", type=float, default=0.035)
    parser.add_argument("--snap-strength", type=float, default=0.62)
    parser.add_argument("--throw-threshold", type=float, default=0.22)
    return parser.parse_args()


def calibration(args: argparse.Namespace, root: Path) -> PointerCalibration:
    path = root / "runtime-data" / "spatial" / "pointer_calibration.json"
    base = (
        PointerCalibration.load(path)
        if path.is_file()
        else PointerCalibration(0.12, 0.88, 0.12, 0.88, True, 0.58)
    )
    return PointerCalibration(
        x_min=base.x_min if args.x_min is None else args.x_min,
        x_max=base.x_max if args.x_max is None else args.x_max,
        y_min=base.y_min if args.y_min is None else args.y_min,
        y_max=base.y_max if args.y_max is None else args.y_max,
        mirror_x=False if args.no_mirror_x else base.mirror_x,
        smoothing=base.smoothing if args.smoothing is None else args.smoothing,
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
    cal = calibration(args, root)

    if not model.is_file():
        raise SystemExit(f"hand model missing: {model}")
    if not overlay.is_file():
        raise SystemExit("overlay build missing; build Pangu.OverlayHost first")

    state.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PANGU_HUD_STATE_FILE"] = str(state)
    env["PANGU_OVERLAY_INTERACTIVE"] = "false"
    overlay_process = subprocess.Popen(["dotnet", str(overlay)], cwd=str(root), env=env)

    runtime = AdvancedLiveSpatialDryRunRuntime(
        model_path=model,
        hud_state_path=state,
        camera_index=args.camera,
        mirror_x=cal.mirror_x,
        pointer_x_min=cal.x_min,
        pointer_x_max=cal.x_max,
        pointer_y_min=cal.y_min,
        pointer_y_max=cal.y_max,
        pointer_smoothing=cal.smoothing,
        ray_gain=args.ray_gain,
        snap_radius=args.snap_radius,
        snap_strength=args.snap_strength,
        throw_velocity_threshold=args.throw_threshold,
    )

    try:
        await runtime.start()
        print("PANGU VISION-STYLE PRECISION SPATIAL DRY RUN")
        print("- full index-finger chain drives a filtered pointing ray")
        print("- actionable UIA controls become semantic pointing targets")
        print("- slow near-target motion enters precision target lock")
        print("- small fist movements use reduced-gain clutch drag for exact placement")
        print("- larger fist movements automatically regain speed")
        print("- closed fist can air-grab the active Chrome tab")
        print("- fast fist movement + open palm triggers THROW_TO_CLOSE")
        print("- real closing remains DISABLED")
        print(f"- throw velocity threshold={args.throw_threshold:.2f}")
        print("START:", runtime.diagnostics())
        print("PRECISION START:", runtime.precision_diagnostics())
        await runtime.run(args.seconds)
        print("FINAL:", runtime.diagnostics())
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
