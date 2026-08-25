from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from pathlib import Path

from pangu.spatial_live import LiveSpatialDryRunRuntime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PANGU live gesture + Chrome HUD in dry-run mode")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--no-mirror-x", action="store_true")
    parser.add_argument("--x-min", type=float, default=0.12)
    parser.add_argument("--x-max", type=float, default=0.88)
    parser.add_argument("--y-min", type=float, default=0.12)
    parser.add_argument("--y-max", type=float, default=0.88)
    parser.add_argument("--smoothing", type=float, default=0.38)
    parser.add_argument("--target-padding", type=float, default=0.018)
    return parser.parse_args()


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

    if not model.is_file():
        raise SystemExit(f"hand model missing: {model}")
    if not overlay.is_file():
        raise SystemExit("overlay build missing; run dotnet build apps/overlay-host/Pangu.OverlayHost.csproj -c Release")

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
        mirror_x=not args.no_mirror_x,
        pointer_x_min=args.x_min,
        pointer_x_max=args.x_max,
        pointer_y_min=args.y_min,
        pointer_y_max=args.y_max,
        pointer_smoothing=args.smoothing,
        target_padding=args.target_padding,
    )

    try:
        await runtime.start()
        print("PANGU LIVE SPATIAL DRY RUN")
        print("- Chrome must be foreground for tab targeting")
        print("- POINT moves the calibrated HUD pointer")
        print("- stable GRAB uses a 2-frame hysteresis gate")
        print("- OPEN_PALM releases")
        print("- tab hit-testing has a small camera-friendly acquisition halo")
        print("- real tab closing is DISABLED")
        print(
            f"- pointer calibration x=({args.x_min:.2f},{args.x_max:.2f}) "
            f"y=({args.y_min:.2f},{args.y_max:.2f}) smoothing={args.smoothing:.2f}"
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
