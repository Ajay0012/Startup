from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from .runtime_builder import RuntimeBuilder
from .self_upgrade import OwnerDirectedSelfUpgradeRuntime
from .settings import resolve_application_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pangu-upgrade",
        description=(
            "Owner-directed PANGU self-upgrade. Changes are generated in an isolated git worktree, "
            "validated, tested, and committed to a pangu-self/* branch."
        ),
    )
    parser.add_argument("feature", help="explicit feature or improvement requested by the owner")
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "after the full test gate passes, create a backup branch and fast-forward the current "
            "branch to the verified self-upgrade commit"
        ),
    )
    return parser.parse_args()


async def run(feature: str, apply: bool = False) -> int:
    container = RuntimeBuilder(resolve_application_root()).build()
    upgrader = OwnerDirectedSelfUpgradeRuntime(container.root, container.gemini_provider)
    try:
        result = await upgrader.upgrade(feature, promote=apply)
        print(json.dumps(asdict(result), default=str))
        if result.promoted or (result.tests_passed and not apply):
            return 0
        return 2 if result.normalized_error else 1
    finally:
        await container.gemini_provider.close()


def main() -> int:
    args = parse_args()
    return asyncio.run(run(args.feature, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
