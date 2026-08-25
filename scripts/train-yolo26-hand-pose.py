from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fine-tune YOLO26 pose on 21-point hand keypoints")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default="0")
    parser.add_argument("--model", default="yolo26n-pose.pt")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        from ultralytics import YOLO
    except ImportError as error:
        raise SystemExit(
            "Ultralytics is not installed. Run: python -m pip install ultralytics"
        ) from error

    root = Path(__file__).resolve().parents[1]
    output = root / "models" / "vision" / "yolo26-hand-pose"
    output.mkdir(parents=True, exist_ok=True)

    model = YOLO(args.model)
    result = model.train(
        data="hand-keypoints.yaml",
        epochs=args.epochs,
        imgsz=args.imgsz,
        device=args.device,
        project=str(output),
        name="train",
        exist_ok=True,
    )
    print("TRAINING COMPLETE")
    print("Results:", getattr(result, "save_dir", output / "train"))
    print("Expected checkpoint:", output / "train" / "weights" / "best.pt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
