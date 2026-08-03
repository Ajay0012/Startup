from __future__ import annotations

import argparse
from pathlib import Path

from .runtime import build_runtime


def main() -> None:
    parser = argparse.ArgumentParser(prog="pangu")
    parser.add_argument("command", nargs="+")
    args = parser.parse_args()
    runtime = build_runtime(Path.cwd())
    runtime.start()
    try:
        result = runtime.command(" ".join(args.command))
        print(f"{result.status}: {result.message}")
        print(result.evidence)
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
