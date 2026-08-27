from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fine-tune YOLO26 pose on 21-point hand keypoints")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default="auto", help="auto, cpu, 0, 1, ...")
    parser.add_argument("--batch", type=int, default=-1, help="-1 lets Ultralytics choose automatically")
    parser.add_argument("--model", default="yolo26n-pose.pt")
    return parser.parse_args()


def resolve_device(value: str) -> str:
    if value != "auto":
        return value
    try:
        import torch

        if torch.cuda.is_available():
            return "0"
    except ImportError:
        pass
    return "cpu"


def main() -> int:
    args = parse_args()
    try:
        from ultralytics import YOLO
    except ImportError as error:
        raise SystemExit(
            'Ultralytics is not installed. Run: python -m pip install -e ".[vision-advanced]"'
        ) from error

    root = Path(__file__).resolve().parents[1]
    output = root / "models" / "vision" / "yolo26-hand-pose"
    output.mkdir(parents=True, exist_ok=True)
    device = resolve_device(args.device)

    print("PANGU YOLO26 21-KEYPOINT HAND-POSE TRAINING")
    print("device:", device)
    print("epochs:", args.epochs)
    print("imgsz:", args.imgsz)
    print("model:", args.model)

    model = YOLO(args.model)
    result = model.train(
        data="hand-keypoints.yaml",
        epochs=args.epochs,
        imgsz=args.imgsz,
        device=device,
        batch=args.batch,
        project=str(output),
        name="train",
        exist_ok=True,
    )
    checkpoint = output / "train" / "weights" / "best.pt"
    print("TRAINING COMPLETE")
    print("Results:", getattr(result, "save_dir", output / "train"))
    print("Expected checkpoint:", checkpoint)
    print("Checkpoint exists:", checkpoint.is_file())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
